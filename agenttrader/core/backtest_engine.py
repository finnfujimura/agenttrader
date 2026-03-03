# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import math
import heapq
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np

from agenttrader.core.base_strategy import BaseStrategy
from agenttrader.core.context import BacktestContext, StreamingBacktestContext
from agenttrader.core.fill_model import FillModel
from agenttrader.data.models import ExecutionMode, Market, Platform


PROGRESS_INTERVAL_SECONDS = 2.5
STREAM_BATCH_MARKET_COUNT = 512
LARGE_RUN_MARKET_WARNING = 5000
LARGE_RUN_WORK_UNITS_WARNING = 1_000_000


@dataclass
class BacktestConfig:
    strategy_path: str
    start_date: str
    end_date: str
    initial_cash: float = 10000.0
    schedule_interval_minutes: int = 15
    history_buffer_hours: int = 168
    max_markets: int | None = None
    fidelity: str = "exact_trade"
    execution_mode: ExecutionMode = ExecutionMode.STRICT_PRICE_ONLY


BacktestProgressCallback = Callable[[dict], None]


class SubscriptionCollector:
    """
    Lightweight context for phase-1 subscription discovery.
    Only subscribe() is functional.
    """

    def __init__(self, market_map: dict[str, Market]):
        self._market_map = market_map
        self._subscribed_ids: set[str] = set()

    def subscribe(
        self,
        platform: str = "all",
        category: str | None = None,
        tags: list[str] | None = None,
        market_ids: list[str] | None = None,
    ) -> None:
        if market_ids:
            self._subscribed_ids.update(market_ids)
            return
        for market_id, market in self._market_map.items():
            if platform != "all" and market.platform.value != platform:
                continue
            if category and market.category != category:
                continue
            if tags and not any(tag in market.tags for tag in tags):
                continue
            self._subscribed_ids.add(market_id)

    def get_subscribed_ids(self) -> list[str]:
        return list(self._subscribed_ids)

    def search_markets(self, query, platform="all"):  # noqa: ANN001
        return []

    def get_price(self, market_id):  # noqa: ANN001
        return 0.5

    def get_orderbook(self, market_id):  # noqa: ANN001
        return None

    def get_history(self, market_id, lookback_hours=24):  # noqa: ANN001
        return []

    def get_position(self, market_id):  # noqa: ANN001
        return None

    def get_cash(self):
        return 0.0

    def get_portfolio_value(self):
        return 0.0

    def buy(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return "noop"

    def sell(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return "noop"

    def log(self, message):  # noqa: ANN001
        return None

    def set_state(self, key, value):  # noqa: ANN001
        return None

    def get_state(self, key, default=None):  # noqa: ANN001
        return default


class BacktestEngine:
    def __init__(self, data_source=None, orderbook_store=None):
        self._data = data_source
        self._ob_store = orderbook_store
        self._fill_model = FillModel()

    def run(
        self,
        strategy_class: type,
        config: BacktestConfig,
        progress_callback: BacktestProgressCallback | None = None,
    ) -> dict:
        from agenttrader.data.index_adapter import BacktestIndexAdapter

        index = BacktestIndexAdapter()
        if index.is_available():
            try:
                result = self._run_streaming(strategy_class, config, index, progress_callback=progress_callback)
            finally:
                index.close()

            # If the index had no data for this range, fall back to legacy
            if isinstance(result, dict) and not result.get("ok", True):
                error = result.get("error", "")
                if error in ("NoDataInRange", "NoSubscriptions", "DatasetNotFound"):
                    legacy = self._run_legacy(strategy_class, config, progress_callback=progress_callback)
                    if isinstance(legacy, dict) and legacy.get("ok"):
                        legacy["fallback_from"] = "normalized-index"
                        legacy["fallback_reason"] = result.get("message", error)
                    return legacy
            return result
        return self._run_legacy(strategy_class, config, progress_callback=progress_callback)

    def _ensure_legacy_data_source(self) -> None:
        if self._data is not None:
            return
        from agenttrader.data.cache import DataCache
        from agenttrader.data.orderbook_store import OrderBookStore
        from agenttrader.data.parquet_adapter import ParquetDataAdapter
        from agenttrader.db import get_engine

        adapter = ParquetDataAdapter()
        if adapter.is_available():
            self._data = adapter
            self._ob_store = None
            return
        self._data = DataCache(get_engine())
        self._ob_store = OrderBookStore()

    def _emit_progress(
        self,
        progress_callback: BacktestProgressCallback | None,
        payload: dict | None,
    ) -> None:
        if progress_callback is None or payload is None:
            return
        progress_callback(payload)

    def _build_resampled_warning(self, fidelity: str) -> list[str]:
        if fidelity not in {"bar_1h", "bar_1d"}:
            return []
        return [
            (
                "Using resampled bars. Strategies that rely on get_history(lookback_hours=...) "
                "may see lower-density history and may need recalibration."
            )
        ]

    def _build_preflight_payload(
        self,
        *,
        data_source: str,
        fidelity: str,
        start_ts: int,
        end_ts: int,
        markets_tested: int,
        max_markets_applied: int | None,
        requested_max_markets: int | None,
        estimated_work_units: int | None,
        work_unit_label: str,
        warnings: list[str] | None = None,
    ) -> dict:
        payload = {
            "kind": "preflight",
            "status": "running",
            "data_source": data_source,
            "fidelity": fidelity,
            "range_start_ts": start_ts,
            "range_end_ts": end_ts,
            "markets_tested": markets_tested,
            "max_markets_applied": max_markets_applied,
            "requested_max_markets": requested_max_markets,
            "estimated_work_units": estimated_work_units,
            "work_unit_label": work_unit_label,
            "warnings": list(warnings or []),
        }
        if requested_max_markets is not None and max_markets_applied is not None:
            payload["max_markets_was_applied"] = True
        elif requested_max_markets is not None:
            payload["max_markets_was_applied"] = False

        if requested_max_markets is None and (
            markets_tested >= LARGE_RUN_MARKET_WARNING
            or (estimated_work_units is not None and estimated_work_units >= LARGE_RUN_WORK_UNITS_WARNING)
        ):
            payload["large_run_warning"] = (
                "Large backtest detected. Consider --max-markets for faster exploratory runs."
            )
        return payload

    def _build_progress_payload(
        self,
        *,
        data_source: str,
        fidelity: str,
        start_ts: int,
        end_ts: int,
        current_ts: int,
        markets_tested: int,
        max_markets_applied: int | None,
        processed_units: int,
        work_unit_label: str,
        wall_start: float,
    ) -> dict:
        elapsed = max(time.perf_counter() - wall_start, 1e-9)
        total_span = max(end_ts - start_ts, 1)
        percent = min(max((current_ts - start_ts) / total_span, 0.0), 1.0) * 100.0
        throughput = processed_units / elapsed
        eta_seconds = None
        if percent > 0.0:
            eta_seconds = max((elapsed / (percent / 100.0)) - elapsed, 0.0)
        return {
            "kind": "progress",
            "status": "running",
            "data_source": data_source,
            "fidelity": fidelity,
            "range_start_ts": start_ts,
            "range_end_ts": end_ts,
            "current_ts": current_ts,
            "markets_tested": markets_tested,
            "max_markets_applied": max_markets_applied,
            "processed_units": processed_units,
            "work_unit_label": work_unit_label,
            "percent_complete": round(percent, 2),
            "elapsed_seconds": round(elapsed, 3),
            "throughput_per_second": round(throughput, 2),
            "eta_seconds": round(eta_seconds, 3) if eta_seconds is not None else None,
        }

    def _iter_market_chunks(self, markets: list[Market], chunk_size: int = STREAM_BATCH_MARKET_COUNT) -> list[tuple[str, list[str]]]:
        by_platform: dict[str, list[str]] = defaultdict(list)
        for market in markets:
            by_platform[market.platform.value].append(market.id)
        chunks: list[tuple[str, list[str]]] = []
        for platform, market_ids in by_platform.items():
            for start in range(0, len(market_ids), chunk_size):
                chunks.append((platform, market_ids[start:start + chunk_size]))
        return chunks

    def _run_legacy(
        self,
        strategy_class: type,
        config: BacktestConfig,
        progress_callback: BacktestProgressCallback | None = None,
    ) -> dict:
        self._ensure_legacy_data_source()

        start_dt = datetime.strptime(config.start_date, "%Y-%m-%d").replace(tzinfo=UTC)
        end_dt = datetime.strptime(config.end_date, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1) - timedelta(seconds=1)
        start_ts = int(start_dt.timestamp())
        end_ts = int(end_dt.timestamp())

        all_markets = self._data.get_markets(platform="all", limit=10_000)
        market_map: dict[str, Market] = {m.id: m for m in all_markets}

        subscription_collector = SubscriptionCollector(market_map)
        strategy_probe: BaseStrategy = strategy_class(subscription_collector)
        strategy_probe.on_start()

        subscribed_ids = subscription_collector.get_subscribed_ids()
        subscribed_markets = [market_map[mid] for mid in subscribed_ids if mid in market_map]
        if not subscribed_markets:
            raise ValueError(
                "Strategy subscribed to 0 markets after on_start(). "
                "Make sure your strategy calls self.subscribe() in on_start()."
            )

        markets_by_id: dict[str, Market] = {m.id: m for m in subscribed_markets}

        from agenttrader.data.parquet_adapter import ParquetDataAdapter

        using_parquet = isinstance(self._data, ParquetDataAdapter)
        price_data: dict[str, list] = {}
        orderbook_data: dict[str, list] = {}
        platform_map: dict[str, Platform] = {}
        for market in subscribed_markets:
            if using_parquet:
                history = self._data.get_price_history(market.id, market.platform, start_ts, end_ts)
            else:
                history = self._data.get_price_history(market.id, start_ts, end_ts)
            if not history:
                continue
            price_data[market.id] = history
            platform_map[market.id] = market.platform
            if not using_parquet and self._ob_store is not None:
                orderbook_data[market.id] = self._ob_store.read(market.platform.value, market.id, start_ts, end_ts)

        context = BacktestContext(
            initial_cash=config.initial_cash,
            price_data=price_data,
            orderbook_data=orderbook_data,
            markets={mid: markets_by_id[mid] for mid in price_data.keys() if mid in markets_by_id},
            parquet_adapter=self._data if using_parquet else None,
            platform_map=platform_map,
        )
        context.advance_time(start_ts)

        strategy: BaseStrategy = strategy_class(context)
        strategy.on_start()

        subscribed = context._subscriptions if context._subscriptions else set(price_data.keys())
        events: list[dict] = []

        for market_id in subscribed:
            points = price_data.get(market_id, [])
            for point in points:
                events.append(
                    {
                        "type": "price_update",
                        "timestamp": point.timestamp,
                        "market_id": market_id,
                        "price": point.yes_price,
                    }
                )

        tick_ts = start_ts
        while tick_ts <= end_ts:
            for market_id in subscribed:
                events.append({"type": "schedule_tick", "timestamp": tick_ts, "market_id": market_id})
            tick_ts += config.schedule_interval_minutes * 60

        for market_id in subscribed:
            market = markets_by_id.get(market_id)
            if market and market.resolved and start_ts <= market.close_time <= end_ts:
                events.append(
                    {
                        "type": "resolution",
                        "timestamp": market.close_time,
                        "market_id": market_id,
                        "outcome": market.resolution or "no",
                    }
                )

        type_priority = {"price_update": 0, "schedule_tick": 1, "resolution": 2}
        events.sort(key=lambda e: (e["timestamp"], type_priority[e["type"]]))

        self._emit_progress(
            progress_callback,
            self._build_preflight_payload(
                data_source="parquet" if isinstance(self._data, ParquetDataAdapter) else "sqlite",
                fidelity="exact_trade",
                start_ts=start_ts,
                end_ts=end_ts,
                markets_tested=len(price_data),
                max_markets_applied=None,
                requested_max_markets=config.max_markets,
                estimated_work_units=len(events),
                work_unit_label="events",
            ),
        )
        wall_start = time.perf_counter()
        last_progress_emit = wall_start
        processed_units = 0

        for event in events:
            context.advance_time(event["timestamp"])
            market = markets_by_id.get(event["market_id"])
            # Set active market to prevent cross-market look-ahead bias
            context.set_active_market(event["market_id"])
            if event["type"] == "price_update":
                if market:
                    strategy.on_market_data(market, float(event["price"]), context.get_orderbook(event["market_id"]))
            elif event["type"] == "schedule_tick":
                if market:
                    strategy.on_schedule(datetime.fromtimestamp(event["timestamp"], tz=UTC), market)
            elif event["type"] == "resolution":
                if market:
                    pnl = context.settle_positions(event["market_id"], event["outcome"])
                    strategy.on_resolution(market, event["outcome"], pnl)
            context.set_active_market(None)
            context.record_snapshot()
            processed_units += 1
            now_perf = time.perf_counter()
            if now_perf - last_progress_emit >= PROGRESS_INTERVAL_SECONDS:
                self._emit_progress(
                    progress_callback,
                    self._build_progress_payload(
                        data_source="parquet" if isinstance(self._data, ParquetDataAdapter) else "sqlite",
                        fidelity="exact_trade",
                        start_ts=start_ts,
                        end_ts=end_ts,
                        current_ts=int(event["timestamp"]),
                        markets_tested=len(price_data),
                        max_markets_applied=None,
                        processed_units=processed_units,
                        work_unit_label="events",
                        wall_start=wall_start,
                    ),
                )
                last_progress_emit = now_perf

        strategy.on_stop()

        raw = context.compile_results()
        metrics = self._compute_metrics(raw["equity_curve"], raw["trades"])
        resolution_accuracy = self._compute_resolution_accuracy(raw["trades"])
        by_category = self._compute_by_category(raw["trades"], markets_by_id, config.initial_cash)
        return {
            "ok": True,
            "strategy_path": config.strategy_path,
            "start_date": config.start_date,
            "end_date": config.end_date,
            "initial_cash": config.initial_cash,
            "data_source": "parquet" if isinstance(self._data, ParquetDataAdapter) else "sqlite",
            "fidelity": "exact_trade",
            "max_markets_applied": None,
            "markets_tested": len(price_data),
            "final_value": raw["final_value"],
            "metrics": metrics,
            "resolution_accuracy": resolution_accuracy,
            "by_category": by_category,
            "execution_mode": config.execution_mode.value,
            "equity_curve": raw["equity_curve"],
            "trades": raw["trades"],
        }

    def _run_streaming(
        self,
        strategy_class: type,
        config: BacktestConfig,
        index,
        progress_callback: BacktestProgressCallback | None = None,
    ) -> dict:
        from agenttrader.data.parquet_adapter import ParquetDataAdapter

        start_dt = datetime.strptime(config.start_date, "%Y-%m-%d").replace(tzinfo=UTC)
        end_dt = datetime.strptime(config.end_date, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1) - timedelta(seconds=1)
        start_ts = int(start_dt.timestamp())
        end_ts = int(end_dt.timestamp())

        if hasattr(index, "get_market_ids_with_counts"):
            candidate_rows = index.get_market_ids_with_counts(platform="all", start_ts=start_ts, end_ts=end_ts)
        else:
            candidate_rows = [
                (market_id, market_platform, 0)
                for market_id, market_platform in index.get_market_ids(platform="all", start_ts=start_ts, end_ts=end_ts)
            ]
        if not candidate_rows:
            return {
                "ok": False,
                "error": "NoDataInRange",
                "message": "No normalized data available for the requested date range. Run dataset build-index or adjust dates.",
            }

        parquet = ParquetDataAdapter()
        if not parquet.is_available():
            return {
                "ok": False,
                "error": "DatasetNotFound",
                "message": "Parquet metadata unavailable. Run: agenttrader dataset download",
            }

        all_markets = parquet.get_markets(platform="all", limit=50_000)
        candidate_ids = {market_id for market_id, _platform, _count in candidate_rows}
        counts = {market_id: int(count) for market_id, _platform, count in candidate_rows}
        market_map: dict[str, Market] = {m.id: m for m in all_markets if m.id in candidate_ids}
        if not market_map:
            market_map = {m.id: m for m in all_markets}

        collector = SubscriptionCollector(market_map)
        probe = strategy_class(collector)
        probe.on_start()
        subscribed_ids = set(collector.get_subscribed_ids())

        final_ids = subscribed_ids & candidate_ids
        subscribed_markets = [market_map[mid] for mid in final_ids if mid in market_map]
        if not subscribed_markets:
            return {
                "ok": False,
                "error": "NoSubscriptions",
                "message": (
                    "Strategy subscribed to 0 markets with data in the requested date range. "
                    "Check your subscribe() call and date range."
                ),
            }

        max_markets_applied = None
        if config.max_markets is not None and len(subscribed_markets) > config.max_markets:
            subscribed_markets.sort(key=lambda m: counts.get(m.id, 0), reverse=True)
            subscribed_markets = subscribed_markets[: config.max_markets]
            max_markets_applied = int(config.max_markets)

        fidelity = getattr(config, "fidelity", "exact_trade") or "exact_trade"
        if fidelity not in {"exact_trade", "bar_1h", "bar_1d"}:
            fidelity = "exact_trade"
        bar_seconds = 3600 if fidelity == "bar_1h" else 86400 if fidelity == "bar_1d" else None
        work_unit_label = "bars" if bar_seconds is not None else "events"
        if bar_seconds is None:
            estimated_work_units = sum(counts.get(m.id, 0) for m in subscribed_markets)
        else:
            estimated_work_units = len(subscribed_markets) * max(1, math.ceil((end_ts - start_ts + 1) / bar_seconds))
        warnings = self._build_resampled_warning(fidelity)
        self._emit_progress(
            progress_callback,
            self._build_preflight_payload(
                data_source="normalized-index",
                fidelity=fidelity,
                start_ts=start_ts,
                end_ts=end_ts,
                markets_tested=len(subscribed_markets),
                max_markets_applied=max_markets_applied,
                requested_max_markets=config.max_markets,
                estimated_work_units=estimated_work_units,
                work_unit_label=work_unit_label,
                warnings=warnings,
            ),
        )

        subscribed_map = {m.id: m for m in subscribed_markets}
        context = StreamingBacktestContext(
            initial_cash=config.initial_cash,
            market_map=subscribed_map,
            fill_model=self._fill_model,
            history_buffer_hours=config.history_buffer_hours,
            execution_mode=config.execution_mode,
        )
        context._subscriptions = set(subscribed_map.keys())
        context.advance_time(start_ts)
        context.record_snapshot(start_ts)

        strategy = strategy_class(context)
        strategy.on_start()
        batch_enabled = all(
            [
                hasattr(index, "stream_market_history_batch"),
                hasattr(index, "stream_market_history_resampled_batch"),
                hasattr(index, "get_latest_prices_before_batch"),
            ]
        )

        chunk_specs = self._iter_market_chunks(subscribed_markets)
        heap: list[tuple[int, str, str, object]] = []
        iterators: dict[str, object] = {}

        if batch_enabled:
            for chunk_idx, (platform, market_ids) in enumerate(chunk_specs):
                previous_map = index.get_latest_prices_before_batch(market_ids, platform, start_ts)
                for market_id, previous in previous_map.items():
                    context.set_price_cursor(market_id, previous)

                iterator_key = f"chunk-{chunk_idx}"
                if bar_seconds is not None:
                    iterators[iterator_key] = index.stream_market_history_resampled_batch(
                        market_ids,
                        platform,
                        start_ts,
                        end_ts,
                        bar_seconds,
                    )
                else:
                    iterators[iterator_key] = index.stream_market_history_batch(
                        market_ids,
                        platform,
                        start_ts,
                        end_ts,
                    )
                first = next(iterators[iterator_key], None)
                if first is not None:
                    market_id, point = first
                    heapq.heappush(heap, (int(point.timestamp), market_id, iterator_key, point))
        else:
            for market in subscribed_markets:
                previous = index.get_latest_price_before(market.id, market.platform.value, start_ts)
                if previous is not None:
                    context.set_price_cursor(market.id, previous)

                if bar_seconds is not None:
                    iterator = index.stream_market_history_resampled(
                        market.id,
                        market.platform.value,
                        start_ts,
                        end_ts,
                        bar_seconds,
                    )
                else:
                    iterator = index.stream_market_history(
                        market.id,
                        market.platform.value,
                        start_ts,
                        end_ts,
                    )
                iterators[market.id] = iterator
                first = next(iterator, None)
                if first is not None:
                    heapq.heappush(heap, (int(first.timestamp), market.id, market.id, first))

        schedule_interval = max(int(config.schedule_interval_minutes), 1) * 60
        last_scheduled = {m.id: start_ts for m in subscribed_markets}
        last_snapshot_ts = start_ts
        wall_start = time.perf_counter()
        last_progress_emit = wall_start
        processed_units = 0

        while heap:
            ts, market_id, iterator_key, point = heapq.heappop(heap)
            if ts < start_ts or ts > end_ts:
                continue
            market = subscribed_map.get(market_id)
            if market is None:
                continue

            context.advance_time(int(ts))
            context.set_active_market(market_id)
            context.set_price_cursor(market_id, float(point.yes_price))
            context.push_history(market_id, point)
            if config.execution_mode == ExecutionMode.SYNTHETIC_EXECUTION_MODEL:
                ob = context.get_orderbook(market_id)
            else:
                ob = None
            strategy.on_market_data(market, float(point.yes_price), ob)

            if ts - last_scheduled[market_id] >= schedule_interval:
                strategy.on_schedule(datetime.fromtimestamp(ts, tz=UTC), market)
                last_scheduled[market_id] = ts

            if context.portfolio_changed_since_last_check() or ts - last_snapshot_ts >= 3600:
                context.record_snapshot(ts)
                last_snapshot_ts = ts

            nxt = next(iterators[iterator_key], None)
            if nxt is not None:
                if batch_enabled:
                    next_market_id, next_point = nxt
                    heapq.heappush(heap, (int(next_point.timestamp), next_market_id, iterator_key, next_point))
                else:
                    heapq.heappush(heap, (int(nxt.timestamp), market_id, iterator_key, nxt))
            context.set_active_market(None)
            processed_units += 1
            now_perf = time.perf_counter()
            if now_perf - last_progress_emit >= PROGRESS_INTERVAL_SECONDS:
                self._emit_progress(
                    progress_callback,
                    self._build_progress_payload(
                        data_source="normalized-index",
                        fidelity=fidelity,
                        start_ts=start_ts,
                        end_ts=end_ts,
                        current_ts=int(ts),
                        markets_tested=len(subscribed_markets),
                        max_markets_applied=max_markets_applied,
                        processed_units=processed_units,
                        work_unit_label=work_unit_label,
                        wall_start=wall_start,
                    ),
                )
                last_progress_emit = now_perf

        for market in subscribed_markets:
            if not market.resolved or not market.resolution:
                continue
            if not (start_ts <= int(market.close_time or 0) <= end_ts):
                continue
            close_ts = int(market.close_time or end_ts)
            context.advance_time(max(close_ts, context._current_ts))
            context.set_active_market(market.id)
            pnl = context.settle_positions(market.id, market.resolution)
            strategy.on_resolution(market, market.resolution, pnl)
            context.set_active_market(None)

        strategy.on_stop()
        context.record_snapshot(end_ts)

        raw = context.compile_results()
        metrics = self._compute_metrics(raw["equity_curve"], raw["trades"])
        resolution_accuracy = self._compute_resolution_accuracy(raw["trades"])
        by_category = self._compute_by_category(raw["trades"], subscribed_map, config.initial_cash)
        return {
            "ok": True,
            "strategy_path": config.strategy_path,
            "start_date": config.start_date,
            "end_date": config.end_date,
            "initial_cash": config.initial_cash,
            "data_source": "normalized-index",
            "fidelity": fidelity,
            "max_markets_applied": max_markets_applied,
            "markets_tested": len(subscribed_markets),
            "final_value": raw["final_value"],
            "metrics": metrics,
            "resolution_accuracy": resolution_accuracy,
            "by_category": by_category,
            "execution_mode": config.execution_mode.value,
            "_artifact_payload": {
                "equity_curve": raw["equity_curve"],
                "trades": raw["trades"],
            },
            "warnings": warnings,
        }

    def _compute_metrics(self, equity_curve: list[dict], trades: list[dict]) -> dict:
        if not equity_curve:
            return {
                "total_return_pct": 0.0,
                "annualized_return_pct": 0.0,
                "sharpe_ratio": 0.0,
                "sortino_ratio": 0.0,
                "max_drawdown_pct": 0.0,
                "win_rate": None,
                "profit_factor": None,
                "calmar_ratio": 0.0,
                "total_trades": len(trades),
                "closed_trades": 0,
                "open_positions_at_end": sum(1 for t in trades if t.get("action") == "buy"),
                "avg_slippage": 0.0,
            }

        values = np.array([float(p["value"]) for p in equity_curve], dtype=float)
        timestamps = np.array([int(p["timestamp"]) for p in equity_curve], dtype=float)
        initial = values[0] if values[0] != 0.0 else 1.0
        final = values[-1]
        total_return = (final / initial - 1.0) * 100.0

        years = max((timestamps[-1] - timestamps[0]) / (365.25 * 24 * 3600), 1 / 365.25)
        annualized_return = ((final / initial) ** (1 / years) - 1.0) * 100.0

        returns = np.diff(values) / np.where(values[:-1] == 0, 1.0, values[:-1])
        sharpe = 0.0
        sortino = 0.0
        if returns.size > 1:
            mean_r = float(np.mean(returns))
            std_r = float(np.std(returns))
            downside = returns[returns < 0]
            downside_std = float(np.std(downside)) if downside.size else 0.0
            scale = math.sqrt(365 * 24)  # hourly-ish normalization
            sharpe = (mean_r / std_r * scale) if std_r > 0 else 0.0
            sortino = (mean_r / downside_std * scale) if downside_std > 0 else 0.0

        rolling_max = np.maximum.accumulate(values)
        drawdowns = (values - rolling_max) / np.where(rolling_max == 0, 1.0, rolling_max)
        max_drawdown_pct = float(np.min(drawdowns) * 100.0)

        closed = [t for t in trades if t.get("action") in {"sell", "resolution"} and t.get("pnl") is not None]
        wins = [t for t in closed if float(t["pnl"]) > 0]
        losses = [t for t in closed if float(t["pnl"]) < 0]
        win_rate = (len(wins) / len(closed)) if closed else None
        gross_profit = sum(float(t["pnl"]) for t in wins)
        gross_loss = abs(sum(float(t["pnl"]) for t in losses))
        if not closed:
            profit_factor = None
        elif gross_loss > 0:
            profit_factor = gross_profit / gross_loss
        elif gross_profit > 0:
            profit_factor = float("inf")
        else:
            profit_factor = 0.0
        calmar = (annualized_return / abs(max_drawdown_pct)) if max_drawdown_pct < 0 else 0.0

        slippage = [abs(float(t.get("slippage", 0.0))) for t in trades if t.get("action") in {"buy", "sell"}]
        buys = sum(1 for t in trades if t.get("action") == "buy")
        sells = sum(1 for t in trades if t.get("action") in {"sell", "resolution"})

        return {
            "total_return_pct": round(total_return, 4),
            "annualized_return_pct": round(annualized_return, 4),
            "sharpe_ratio": round(sharpe, 4),
            "sortino_ratio": round(sortino, 4),
            "max_drawdown_pct": round(max_drawdown_pct, 4),
            "win_rate": round(win_rate, 4) if win_rate is not None else None,
            "profit_factor": (
                round(profit_factor, 4)
                if profit_factor is not None and math.isfinite(profit_factor)
                else profit_factor
            ),
            "calmar_ratio": round(calmar, 4),
            "total_trades": len(trades),
            "closed_trades": len(closed),
            "open_positions_at_end": max(buys - sells, 0),
            "avg_slippage": round(float(np.mean(slippage)) if slippage else 0.0, 6),
        }

    def _compute_resolution_accuracy(self, trades: list[dict]) -> dict:
        bought_yes = [
            t
            for t in trades
            if t.get("action") == "buy"
            and str(t.get("side", "")).lower() == "yes"
            and t.get("resolved_correctly") is not None
        ]
        bought_no = [
            t
            for t in trades
            if t.get("action") == "buy"
            and str(t.get("side", "")).lower() == "no"
            and t.get("resolved_correctly") is not None
        ]

        sample_size = len(bought_yes) + len(bought_no)
        if sample_size == 0:
            return {
                "bought_yes_resolved_yes_pct": None,
                "bought_no_resolved_no_pct": None,
                "sample_size": None,
                "note": "No markets resolved in this date range. Use --resolved flag when syncing to increase sample.",
            }

        yes_correct = sum(1 for t in bought_yes if t.get("resolved_correctly") is True)
        no_correct = sum(1 for t in bought_no if t.get("resolved_correctly") is True)
        return {
            "bought_yes_resolved_yes_pct": round(yes_correct / len(bought_yes), 3) if bought_yes else None,
            "bought_no_resolved_no_pct": round(no_correct / len(bought_no), 3) if bought_no else None,
            "sample_size": sample_size,
        }

    def _compute_by_category(self, trades: list[dict], markets: dict[str, Market], initial_cash: float) -> dict:
        buckets = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})

        for trade in trades:
            if trade.get("action") not in {"sell", "resolution"}:
                continue
            pnl = float(trade.get("pnl") or 0.0)
            market = markets.get(str(trade.get("market_id", "")))
            category = (market.category if market is not None else "") or "unknown"

            buckets[category]["trades"] += 1
            if pnl > 0:
                buckets[category]["wins"] += 1
            buckets[category]["pnl"] += pnl

        result: dict[str, dict] = {}
        for category in sorted(buckets.keys()):
            data = buckets[category]
            trade_count = data["trades"]
            result[category] = {
                "trades": trade_count,
                "win_rate": round(data["wins"] / trade_count, 3) if trade_count > 0 else None,
                "return_pct": round((data["pnl"] / initial_cash) * 100.0, 2) if initial_cash > 0 else None,
            }
        return result
