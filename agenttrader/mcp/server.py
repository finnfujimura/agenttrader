# DO NOT import dome_api_sdk here. Use agenttrader.data.dome_client only.
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from agenttrader.cli.validate import validate_strategy_file
from agenttrader.config import load_config
from agenttrader.core.backtest_engine import BacktestConfig, BacktestEngine
from agenttrader.core.base_strategy import BaseStrategy
from agenttrader.core.paper_daemon import PaperDaemon
from agenttrader.data.cache import DataCache
from agenttrader.data.dome_client import DomeClient
from agenttrader.data.orderbook_store import OrderBookStore
from agenttrader.db import get_engine, get_session
from agenttrader.db.schema import BacktestRun, PaperPortfolio


server = Server("agenttrader")


def _text(obj) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(obj, default=str))]


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_markets",
            description="List prediction markets from local cache. Filter by platform, category, tags.",
            inputSchema={
                "type": "object",
                "properties": {
                    "platform": {"type": "string", "enum": ["polymarket", "kalshi", "all"], "default": "all"},
                    "category": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer", "default": 20},
                },
            },
        ),
        types.Tool(name="get_price", description="Get latest cached price", inputSchema={"type": "object", "properties": {"market_id": {"type": "string"}}, "required": ["market_id"]}),
        types.Tool(name="get_history", description="Get cached price history", inputSchema={"type": "object", "properties": {"market_id": {"type": "string"}, "days": {"type": "integer", "default": 7}}, "required": ["market_id"]}),
        types.Tool(name="match_markets", description="Match markets across platforms", inputSchema={"type": "object", "properties": {"polymarket_slug": {"type": "string"}, "kalshi_ticker": {"type": "string"}}}),
        types.Tool(name="run_backtest", description="Run a strategy backtest", inputSchema={"type": "object", "properties": {"strategy_path": {"type": "string"}, "start_date": {"type": "string"}, "end_date": {"type": "string"}, "initial_cash": {"type": "number", "default": 10000}}, "required": ["strategy_path", "start_date", "end_date"]}),
        types.Tool(name="get_backtest", description="Get backtest by run id", inputSchema={"type": "object", "properties": {"run_id": {"type": "string"},}, "required": ["run_id"]}),
        types.Tool(name="list_backtests", description="List recent backtest runs", inputSchema={"type": "object", "properties": {}}),
        types.Tool(name="validate_strategy", description="Validate strategy file", inputSchema={"type": "object", "properties": {"strategy_path": {"type": "string"}}, "required": ["strategy_path"]}),
        types.Tool(name="start_paper_trade", description="Start paper trading daemon", inputSchema={"type": "object", "properties": {"strategy_path": {"type": "string"}, "initial_cash": {"type": "number", "default": 10000}}, "required": ["strategy_path"]}),
        types.Tool(name="get_portfolio", description="Get paper portfolio status", inputSchema={"type": "object", "properties": {"portfolio_id": {"type": "string"}}, "required": ["portfolio_id"]}),
        types.Tool(name="stop_paper_trade", description="Stop paper trading daemon", inputSchema={"type": "object", "properties": {"portfolio_id": {"type": "string"}}, "required": ["portfolio_id"]}),
        types.Tool(name="list_paper_trades", description="List paper portfolios", inputSchema={"type": "object", "properties": {}}),
        types.Tool(name="sync_data", description="Sync data from Dome", inputSchema={"type": "object", "properties": {"days": {"type": "integer", "default": 7}, "platform": {"type": "string", "default": "all"}, "market_ids": {"type": "array", "items": {"type": "string"}}, "limit": {"type": "integer", "default": 100}}}),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    args = arguments or {}
    cache = DataCache(get_engine())

    if name == "get_markets":
        markets = cache.get_markets(
            platform=args.get("platform", "all"),
            category=args.get("category"),
            tags=args.get("tags"),
            limit=int(args.get("limit", 20)),
        )
        return _text({"ok": True, "count": len(markets), "markets": [m.__dict__ | {"platform": m.platform.value, "market_type": m.market_type.value} for m in markets]})

    if name == "get_price":
        market_id = args["market_id"]
        latest = cache.get_latest_price(market_id)
        return _text({"ok": bool(latest), "market_id": market_id, "price": latest.__dict__ if latest else None})

    if name == "get_history":
        import time

        market_id = args["market_id"]
        days = int(args.get("days", 7))
        end_ts = int(time.time())
        start_ts = end_ts - days * 24 * 3600
        history = cache.get_price_history(market_id, start_ts, end_ts)
        return _text({"ok": True, "market_id": market_id, "history": [h.__dict__ for h in history]})

    if name == "match_markets":
        cfg = load_config()
        client = DomeClient(str(cfg.get("dome_api_key", "")))
        matches = client.get_matching_markets(args.get("polymarket_slug"), args.get("kalshi_ticker"))
        return _text({"ok": True, "matches": matches})

    if name == "validate_strategy":
        return _text(validate_strategy_file(args["strategy_path"]))

    if name == "run_backtest":
        strategy_path = Path(args["strategy_path"])
        run_id = str(uuid.uuid4())
        strategy_hash = hashlib.sha256(strategy_path.read_bytes()).hexdigest()
        now_ts = int(datetime.now(tz=UTC).timestamp())

        with get_session(get_engine()) as session:
            session.add(
                BacktestRun(
                    id=run_id,
                    strategy_path=str(strategy_path),
                    strategy_hash=strategy_hash,
                    start_date=args["start_date"],
                    end_date=args["end_date"],
                    initial_cash=float(args.get("initial_cash", 10000.0)),
                    status="running",
                    created_at=now_ts,
                )
            )
            session.commit()

        spec = importlib.util.spec_from_file_location("user_strategy", str(strategy_path))
        module = importlib.util.module_from_spec(spec)
        assert spec is not None and spec.loader is not None
        spec.loader.exec_module(module)
        strategy_class = next(
            cls
            for _, cls in inspect.getmembers(module, inspect.isclass)
            if issubclass(cls, BaseStrategy) and cls is not BaseStrategy
        )
        bt = BacktestEngine(cache, OrderBookStore())
        result = bt.run(
            strategy_class,
            BacktestConfig(
                strategy_path=str(strategy_path),
                start_date=args["start_date"],
                end_date=args["end_date"],
                initial_cash=float(args.get("initial_cash", 10000.0)),
                schedule_interval_minutes=int(load_config().get("schedule_interval_minutes", 15)),
            ),
        )
        result["run_id"] = run_id

        with get_session(get_engine()) as session:
            row = session.get(BacktestRun, run_id)
            if row:
                row.status = "complete"
                row.results_json = json.dumps(result)
                row.completed_at = int(datetime.now(tz=UTC).timestamp())
                session.commit()

        return _text(result)

    if name == "get_backtest":
        row = cache.get_backtest_run(args["run_id"])
        if not row:
            return _text({"ok": False, "error": "NotFound", "message": "run not found"})
        return _text(json.loads(row.results_json) if row.results_json else {"ok": True, "status": row.status})

    if name == "list_backtests":
        rows = cache.list_backtest_runs(limit=100)
        return _text({"ok": True, "runs": [{"id": r.id, "status": r.status, "strategy_path": r.strategy_path} for r in rows]})

    if name == "start_paper_trade":
        strategy_path = Path(args["strategy_path"])
        portfolio_id = str(uuid.uuid4())
        initial_cash = float(args.get("initial_cash", 10000.0))
        with get_session(get_engine()) as session:
            session.add(
                PaperPortfolio(
                    id=portfolio_id,
                    strategy_path=str(strategy_path),
                    strategy_hash=hashlib.sha256(strategy_path.read_bytes()).hexdigest(),
                    initial_cash=initial_cash,
                    cash_balance=initial_cash,
                    status="running",
                    started_at=int(datetime.now(tz=UTC).timestamp()),
                    reload_count=0,
                )
            )
            session.commit()
        daemon = PaperDaemon(portfolio_id, str(strategy_path), initial_cash)
        pid = daemon.start_as_daemon()
        with get_session(get_engine()) as session:
            row = session.get(PaperPortfolio, portfolio_id)
            if row:
                row.pid = pid
                session.commit()
        return _text({"ok": True, "portfolio_id": portfolio_id, "pid": pid})

    if name == "get_portfolio":
        p = cache.get_portfolio(args["portfolio_id"])
        if not p:
            return _text({"ok": False, "error": "NotFound", "message": "portfolio not found"})
        positions = cache.get_open_positions(p.id)
        out = []
        unrealized = 0.0
        for pos in positions:
            latest = cache.get_latest_price(pos.market_id)
            current = latest.yes_price if latest else pos.avg_cost
            pnl = (current - pos.avg_cost) * pos.contracts
            unrealized += pnl
            out.append(
                {
                    "market_id": pos.market_id,
                    "platform": pos.platform,
                    "side": pos.side,
                    "contracts": pos.contracts,
                    "avg_cost": pos.avg_cost,
                    "current_price": current,
                    "unrealized_pnl": pnl,
                }
            )
        return _text(
            {
                "ok": True,
                "portfolio_id": p.id,
                "status": p.status,
                "pid": p.pid,
                "initial_cash": p.initial_cash,
                "cash_balance": p.cash_balance,
                "portfolio_value": p.cash_balance + sum(i["contracts"] * i["current_price"] for i in out),
                "unrealized_pnl": unrealized,
                "positions": out,
                "last_reload": p.last_reload,
                "reload_count": p.reload_count or 0,
            }
        )

    if name == "stop_paper_trade":
        p = cache.get_portfolio(args["portfolio_id"])
        if not p:
            return _text({"ok": False, "error": "NotFound", "message": "portfolio not found"})
        import os
        import signal

        if p.pid:
            try:
                os.kill(int(p.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
        with get_session(get_engine()) as session:
            row = session.get(PaperPortfolio, p.id)
            if row:
                row.status = "stopped"
                row.stopped_at = int(datetime.now(tz=UTC).timestamp())
                session.commit()
        return _text({"ok": True, "portfolio_id": p.id, "stopped": True})

    if name == "list_paper_trades":
        rows = cache.list_paper_portfolios()
        return _text({"ok": True, "portfolios": [{"id": p.id, "status": p.status, "pid": p.pid} for p in rows]})

    if name == "sync_data":
        cfg = load_config()
        client = DomeClient(str(cfg.get("dome_api_key", "")))
        days = int(args.get("days", 7))
        platform = args.get("platform", "all")
        market_ids = args.get("market_ids")
        limit = int(args.get("limit", 100))
        markets = client.get_markets(platform=platform, market_ids=market_ids, limit=limit)

        import time

        start_ts = int(time.time()) - days * 24 * 3600
        end_ts = int(time.time())
        ob_store = OrderBookStore()
        pp = 0
        ob_files = 0
        for m in markets:
            cache.upsert_market(m)
            candles = client.get_candlesticks(m.condition_id, m.platform, start_ts, end_ts, 60)
            for p in candles:
                cache.upsert_price_point(m.id, m.platform.value, p)
            pp += len(candles)
            ob = client.get_orderbook_snapshots(m.id, m.platform, start_ts, end_ts, 100)
            ob_files += ob_store.write(m.platform.value, m.id, ob)
            cache.mark_market_synced(m.id, int(time.time()))

        return _text(
            {
                "ok": True,
                "markets_synced": len(markets),
                "price_points_fetched": pp,
                "orderbook_files_written": ob_files,
                "errors": [],
            }
        )

    return _text({"ok": False, "error": "UnknownTool", "message": name})


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
