# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import math
import heapq
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np

from agenttrader.core.base_strategy import BaseStrategy
from agenttrader.core.context import BacktestContext, StreamingBacktestContext
from agenttrader.core.fill_model import FillModel
from agenttrader.data.models import ExecutionMode, Market, Platform


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

    def run(self, strategy_class: type, config: BacktestConfig) -> dict:
        from agenttrader.data.index_adapter import BacktestIndexAdapter

        index = BacktestIndexAdapter()
        if index.is_available():
            try:
                return self._run_streaming(strategy_class, config, index)
            finally:
                index.close()
        return self._run_legacy(strategy_class, config)

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

    def _run_legacy(self, strategy_class: type, config: BacktestConfig) -> dict:
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

    def _run_streaming(self, strategy_class: type, config: BacktestConfig, index) -> dict:
        from agenttrader.data.parquet_adapter import ParquetDataAdapter

        start_dt = datetime.strptime(config.start_date, "%Y-%m-%d").replace(tzinfo=UTC)
        end_dt = datetime.strptime(config.end_date, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1) - timedelta(seconds=1)
        start_ts = int(start_dt.timestamp())
        end_ts = int(end_dt.timestamp())

        all_candidate_ids = index.get_market_ids(platform="all", start_ts=start_ts, end_ts=end_ts)
        if not all_candidate_ids:
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
        candidate_ids = {market_id for market_id, _platform in all_candidate_ids}
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
            counts = {mid: n for mid, _platform, n in index.get_market_ids_with_counts(platform="all", start_ts=start_ts, end_ts=end_ts)}
            subscribed_markets.sort(key=lambda m: counts.get(m.id, 0), reverse=True)
            subscribed_markets = subscribed_markets[: config.max_markets]
            max_markets_applied = int(config.max_markets)

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

        fidelity = getattr(config, "fidelity", "exact_trade") or "exact_trade"
        if fidelity not in {"exact_trade", "bar_1h", "bar_1d"}:
            fidelity = "exact_trade"
        bar_seconds = 3600 if fidelity == "bar_1h" else 86400 if fidelity == "bar_1d" else None

        def _iter_for(market: Market):
            if bar_seconds is not None:
                return index.stream_market_history_resampled(
                    market.id,
                    market.platform.value,
                    start_ts,
                    end_ts,
                    bar_seconds,
                )
            return index.stream_market_history(
                market.id,
                market.platform.value,
                start_ts,
                end_ts,
            )

        iterators = {m.id: _iter_for(m) for m in subscribed_markets}
        for market in subscribed_markets:
            previous = index.get_latest_price_before(market.id, market.platform.value, start_ts)
            if previous is not None:
                context.set_price_cursor(market.id, previous)

        heap: list[tuple[int, str, object]] = []
        for market in subscribed_markets:
            first = next(iterators[market.id], None)
            if first is not None:
                heapq.heappush(heap, (int(first.timestamp), market.id, first))

        schedule_interval = max(int(config.schedule_interval_minutes), 1) * 60
        last_scheduled = {m.id: start_ts for m in subscribed_markets}
        last_snapshot_ts = start_ts

        while heap:
            ts, market_id, point = heapq.heappop(heap)
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

            nxt = next(iterators[market_id], None)
            if nxt is not None:
                heapq.heappush(heap, (int(nxt.timestamp), market_id, nxt))
            context.set_active_market(None)

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
        }

    def _compute_metrics(self, equity_curve: list[dict], trades: list[dict]) -> dict:
        if not equity_curve:
            return {
                "total_return_pct": 0.0,
                "annualized_return_pct": 0.0,
                "sharpe_ratio": 0.0,
                "sortino_ratio": 0.0,
                "max_drawdown_pct": 0.0,
                "win_rate": 0.0,
                "profit_factor": 0.0,
                "calmar_ratio": 0.0,
                "total_trades": len(trades),
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
        win_rate = (len(wins) / len(closed)) if closed else 0.0
        gross_profit = sum(float(t["pnl"]) for t in wins)
        gross_loss = abs(sum(float(t["pnl"]) for t in losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
        calmar = (annualized_return / abs(max_drawdown_pct)) if max_drawdown_pct < 0 else 0.0

        slippage = [abs(float(t.get("slippage", 0.0))) for t in trades if t.get("action") in {"buy", "sell"}]

        return {
            "total_return_pct": round(total_return, 4),
            "annualized_return_pct": round(annualized_return, 4),
            "sharpe_ratio": round(sharpe, 4),
            "sortino_ratio": round(sortino, 4),
            "max_drawdown_pct": round(max_drawdown_pct, 4),
            "win_rate": round(win_rate, 4),
            "profit_factor": round(profit_factor, 4) if math.isfinite(profit_factor) else float("inf"),
            "calmar_ratio": round(calmar, 4),
            "total_trades": len(trades),
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
