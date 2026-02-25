# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np

from agenttrader.core.base_strategy import BaseStrategy
from agenttrader.core.context import BacktestContext
from agenttrader.data.models import Market


@dataclass
class BacktestConfig:
    strategy_path: str
    start_date: str
    end_date: str
    initial_cash: float = 10000.0
    schedule_interval_minutes: int = 15


class BacktestEngine:
    def __init__(self, cache, orderbook_store):
        self._cache = cache
        self._ob_store = orderbook_store

    def run(self, strategy_class: type, config: BacktestConfig) -> dict:
        start_dt = datetime.strptime(config.start_date, "%Y-%m-%d").replace(tzinfo=UTC)
        end_dt = datetime.strptime(config.end_date, "%Y-%m-%d").replace(tzinfo=UTC) + timedelta(days=1) - timedelta(seconds=1)
        start_ts = int(start_dt.timestamp())
        end_ts = int(end_dt.timestamp())

        markets = self._cache.get_markets(limit=10_000)
        markets_by_id: dict[str, Market] = {m.id: m for m in markets}

        price_data: dict[str, list] = {}
        orderbook_data: dict[str, list] = {}
        for market in markets:
            history = self._cache.get_price_history(market.id, start_ts, end_ts)
            if not history:
                continue
            price_data[market.id] = history
            orderbook_data[market.id] = self._ob_store.read(market.platform.value, market.id, start_ts, end_ts)

        context = BacktestContext(
            initial_cash=config.initial_cash,
            price_data=price_data,
            orderbook_data=orderbook_data,
            markets={mid: markets_by_id[mid] for mid in price_data.keys() if mid in markets_by_id},
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
            "final_value": raw["final_value"],
            "metrics": metrics,
            "resolution_accuracy": resolution_accuracy,
            "by_category": by_category,
            "equity_curve": raw["equity_curve"],
            "trades": raw["trades"],
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
        initial = values[0] if values[0] else 1.0
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
