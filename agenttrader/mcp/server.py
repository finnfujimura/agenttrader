# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import statistics
import sys
import time
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from sqlalchemy.exc import OperationalError

from agenttrader.cli.validate import validate_strategy_file
from agenttrader.config import is_initialized, load_config
from agenttrader.core.backtest_engine import BacktestConfig, BacktestEngine
from agenttrader.core.base_strategy import BaseStrategy
from agenttrader.core.paper_daemon import PaperDaemon
from agenttrader.data.backtest_artifacts import read_backtest_artifact, write_backtest_artifact
from agenttrader.data.cache import DataCache
from agenttrader.data.models import ExecutionMode
from agenttrader.errors import AgentTraderError
from agenttrader.data.pmxt_client import PmxtClient
from agenttrader.data.orderbook_store import OrderBookStore
from agenttrader.data.parquet_adapter import ParquetDataAdapter
from agenttrader.db import get_engine, get_session
from agenttrader.db.schema import BacktestRun, PaperPortfolio
from agenttrader.perf_logging import log_performance_event


server = Server("agenttrader")
MCP_SESSION_ID = f"mcp-{uuid.uuid4().hex[:12]}"


def _text(obj) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(obj, default=str))]


DEFAULT_FIXES = {
    "NotInitialized": "Run: agenttrader init",
    "UnknownTool": "Call list_tools to see supported tool names, then retry.",
    "BadRequest": "Check required tool arguments and retry.",
    "MarketNotCached": "Call sync_data with market_ids=['<market_id>'] (or sync the platform), then retry.",
    "NotFound": "Verify the id exists first (list_backtests or list_paper_trades), then retry.",
}


def _error_payload(error: str, message: str, fix: str | None = None, **extra):
    payload = {
        "ok": False,
        "error": error,
        "message": message,
    }
    resolved_fix = fix or DEFAULT_FIXES.get(error)
    if resolved_fix:
        payload["fix"] = resolved_fix
    payload.update(extra)
    return payload


def _compute_history_analytics(history: list, end_ts: int) -> dict:
    if not history:
        return {
            "current_price": None,
            "avg_7d_price": None,
            "price_vs_7d_avg": None,
            "price_change_24h": None,
            "trend_direction": None,
            "volatility": None,
            "points": 0,
        }

    prices = [float(h.yes_price) for h in history]
    current_price = prices[-1]
    avg_7d = sum(prices) / len(prices)
    cutoff_24h = end_ts - 24 * 3600
    recent_24h = [h for h in history if int(h.timestamp) >= cutoff_24h]
    if recent_24h:
        oldest_24h_price = float(recent_24h[0].yes_price)
        price_change_24h = current_price - oldest_24h_price
    else:
        price_change_24h = None

    if price_change_24h is None:
        trend_direction = None
    elif price_change_24h > 0:
        trend_direction = "up"
    elif price_change_24h < 0:
        trend_direction = "down"
    else:
        trend_direction = "flat"

    return {
        "current_price": current_price,
        "avg_7d_price": avg_7d,
        "price_vs_7d_avg": current_price - avg_7d,
        "price_change_24h": price_change_24h,
        "trend_direction": trend_direction,
        "volatility": statistics.pstdev(prices) if len(prices) > 1 else 0.0,
        "points": len(prices),
    }


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
        types.Tool(name="get_history", description="Get cached market history analytics. Raw history is omitted by default; set include_raw=true to include points.", inputSchema={"type": "object", "properties": {"market_id": {"type": "string"}, "days": {"type": "integer", "default": 7}, "include_raw": {"type": "boolean", "default": False}}, "required": ["market_id"]}),
        types.Tool(name="match_markets", description="Match markets across platforms", inputSchema={"type": "object", "properties": {"polymarket_slug": {"type": "string"}, "kalshi_ticker": {"type": "string"}}}),
        types.Tool(
            name="run_backtest",
            description=(
                "Run a strategy backtest. By default this runs all subscribed markets with exact_trade fidelity. "
                "Set include_curve=true to return full equity/trades arrays. "
                "Use max_markets and fidelity for faster exploratory runs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "strategy_path": {"type": "string"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "initial_cash": {"type": "number", "default": 10000},
                    "max_markets": {
                        "type": "integer",
                        "description": "Optional. Cap number of markets. Default: no limit.",
                    },
                    "fidelity": {
                        "type": "string",
                        "enum": ["exact_trade", "bar_1h", "bar_1d"],
                        "default": "exact_trade",
                        "description": (
                            "exact_trade: every trade (default, most accurate). "
                            "bar_1h: hourly bars (faster). "
                            "bar_1d: daily bars (fastest, least accurate)."
                        ),
                    },
                    "execution_mode": {
                        "type": "string",
                        "enum": ["strict_price_only", "observed_orderbook", "synthetic_execution_model"],
                        "default": "strict_price_only",
                        "description": (
                            "strict_price_only (default): fills at observed prices, no orderbook synthesis. "
                            "observed_orderbook: uses real stored orderbooks, errors if none. "
                            "synthetic_execution_model: synthesizes orderbooks for approximate fills (opt-in)."
                        ),
                    },
                    "include_curve": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, include full equity_curve and trades arrays in response",
                    },
                },
                "required": ["strategy_path", "start_date", "end_date"],
            },
        ),
        types.Tool(name="research_markets", description="Compound research workflow: sync cache, list filtered markets, and return history analytics for each market.", inputSchema={"type": "object", "properties": {"days": {"type": "integer", "default": 7}, "platform": {"type": "string", "enum": ["polymarket", "kalshi", "all"], "default": "all"}, "category": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}, "market_ids": {"type": "array", "items": {"type": "string"}}, "limit": {"type": "integer", "default": 20}, "sync_limit": {"type": "integer", "default": 100}, "include_raw": {"type": "boolean", "default": False}}}),
        types.Tool(
            name="validate_and_backtest",
            description="Compound workflow: validate a strategy file then run backtest when valid.",
            inputSchema={
                "type": "object",
                "properties": {
                    "strategy_path": {"type": "string"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
                    "initial_cash": {"type": "number", "default": 10000},
                    "max_markets": {"type": "integer"},
                    "fidelity": {"type": "string", "enum": ["exact_trade", "bar_1h", "bar_1d"], "default": "exact_trade"},
                    "execution_mode": {
                        "type": "string",
                        "enum": ["strict_price_only", "observed_orderbook", "synthetic_execution_model"],
                        "default": "strict_price_only",
                        "description": (
                            "strict_price_only (default): fills at observed prices, no orderbook synthesis. "
                            "observed_orderbook: uses real stored orderbooks, errors if none. "
                            "synthetic_execution_model: synthesizes orderbooks for approximate fills (opt-in)."
                        ),
                    },
                    "include_curve": {"type": "boolean", "default": False},
                },
                "required": ["strategy_path", "start_date", "end_date"],
            },
        ),
        types.Tool(name="get_backtest", description="Get backtest by run id. Returns metrics only by default. Set include_curve=true to also return the full equity curve and trades array.", inputSchema={"type": "object", "properties": {"run_id": {"type": "string"}, "include_curve": {"type": "boolean", "default": False, "description": "If true, include full equity_curve and trades arrays in response"}}, "required": ["run_id"]}),
        types.Tool(name="list_backtests", description="List recent backtest runs", inputSchema={"type": "object", "properties": {}}),
        types.Tool(name="validate_strategy", description="Validate strategy file", inputSchema={"type": "object", "properties": {"strategy_path": {"type": "string"}}, "required": ["strategy_path"]}),
        types.Tool(name="start_paper_trade", description="Start paper trading daemon", inputSchema={"type": "object", "properties": {"strategy_path": {"type": "string"}, "initial_cash": {"type": "number", "default": 10000}}, "required": ["strategy_path"]}),
        types.Tool(name="get_portfolio", description="Get paper portfolio status", inputSchema={"type": "object", "properties": {"portfolio_id": {"type": "string"}}, "required": ["portfolio_id"]}),
        types.Tool(name="stop_paper_trade", description="Stop paper trading daemon", inputSchema={"type": "object", "properties": {"portfolio_id": {"type": "string"}}, "required": ["portfolio_id"]}),
        types.Tool(name="list_paper_trades", description="List paper portfolios", inputSchema={"type": "object", "properties": {}}),
        types.Tool(name="sync_data", description="Sync data from PMXT", inputSchema={"type": "object", "properties": {"days": {"type": "integer", "default": 7}, "platform": {"type": "string", "default": "all"}, "market_ids": {"type": "array", "items": {"type": "string"}}, "limit": {"type": "integer", "default": 100}}}),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    args = arguments or {}
    started_at = time.time()
    started_perf = time.perf_counter()

    def _respond(payload: dict):
        status = "ok" if bool(payload.get("ok")) else "error"
        log_performance_event(
            source="mcp",
            operation=name,
            started_at=started_at,
            duration_ms=(time.perf_counter() - started_perf) * 1000.0,
            status=status,
            error=payload.get("error"),
            metadata={"argument_keys": sorted(args.keys())},
            session_id=MCP_SESSION_ID,
        )
        return _text(payload)

    if not is_initialized():
        return _respond(
            _error_payload(
                "NotInitialized",
                "agenttrader not initialized",
                fix="Run: agenttrader init",
            )
        )

    cache = DataCache(get_engine())
    try:
        if name == "get_markets":
            markets = cache.get_markets(
                platform=args.get("platform", "all"),
                category=args.get("category"),
                tags=args.get("tags"),
                limit=int(args.get("limit", 20)),
            )
            return _respond({"ok": True, "count": len(markets), "markets": [m.__dict__ | {"platform": m.platform.value, "market_type": m.market_type.value} for m in markets]})

        if name == "get_price":
            market_id = args["market_id"]
            market = cache.get_market(market_id)
            if not market:
                return _respond(
                    _error_payload(
                        "MarketNotCached",
                        f"Market {market_id} not found in local cache",
                        fix=f"Call sync_data(market_ids=['{market_id}']) and retry.",
                    )
                )
            latest = cache.get_latest_price(market_id)
            return _respond({"ok": bool(latest), "market_id": market_id, "price": latest.__dict__ if latest else None})

        if name == "get_history":
            market_id = args["market_id"]
            market = cache.get_market(market_id)
            if not market:
                return _respond(
                    _error_payload(
                        "MarketNotCached",
                        f"Market {market_id} not found in local cache",
                        fix=f"Call sync_data(market_ids=['{market_id}']) and retry.",
                    )
                )
            days = int(args.get("days", 7))
            end_ts = int(time.time())
            start_ts = end_ts - days * 24 * 3600
            history = cache.get_price_history(market_id, start_ts, end_ts)
            include_raw = bool(args.get("include_raw", False))
            payload = {
                "ok": True,
                "market_id": market_id,
                "days": days,
                "analytics": _compute_history_analytics(history, end_ts),
            }
            if include_raw:
                payload["history"] = [h.__dict__ for h in history]
            return _respond(payload)

        if name == "match_markets":
            client = PmxtClient()
            matches = client.get_matching_markets(args.get("polymarket_slug"), args.get("kalshi_ticker"))
            return _respond({"ok": True, "matches": matches})

        if name == "validate_strategy":
            return _respond(validate_strategy_file(args["strategy_path"]))

        if name == "run_backtest":
            strategy_path = Path(args["strategy_path"]).resolve()
            if not strategy_path.exists() or not strategy_path.is_file():
                return _respond(_error_payload("BadRequest", f"Strategy file not found: {args['strategy_path']}"))
            if not str(strategy_path).endswith(".py"):
                return _respond(_error_payload("BadRequest", "Strategy file must be a .py file"))

            # Validate strategy before executing
            validation = validate_strategy_file(str(strategy_path))
            if not validation.get("valid", False):
                return _respond(
                    _error_payload(
                        "StrategyValidationError",
                        "Strategy validation failed",
                        fix="Fix validation errors, then retry run_backtest.",
                        validation=validation,
                    )
                )

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

            try:
                spec = importlib.util.spec_from_file_location("user_strategy", str(strategy_path))
                if spec is None or spec.loader is None:
                    raise RuntimeError(f"Could not load strategy from {strategy_path}")
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                try:
                    spec.loader.exec_module(module)
                except Exception:
                    del sys.modules[spec.name]
                    raise
                strategy_class = None
                for _, cls in inspect.getmembers(module, inspect.isclass):
                    if issubclass(cls, BaseStrategy) and cls is not BaseStrategy:
                        strategy_class = cls
                        break
                if strategy_class is None:
                    raise RuntimeError("No BaseStrategy subclass found in strategy file")

                parquet_adapter = ParquetDataAdapter()
                if parquet_adapter.is_available():
                    bt = BacktestEngine(data_source=parquet_adapter)
                else:
                    bt = BacktestEngine(data_source=cache, orderbook_store=OrderBookStore())
                execution_mode = ExecutionMode(args.get("execution_mode", "strict_price_only"))
                result = bt.run(
                    strategy_class,
                    BacktestConfig(
                        strategy_path=str(strategy_path),
                        start_date=args["start_date"],
                        end_date=args["end_date"],
                        initial_cash=float(args.get("initial_cash", 10000.0)),
                        schedule_interval_minutes=int(load_config().get("schedule_interval_minutes", 15)),
                        max_markets=int(args["max_markets"]) if args.get("max_markets") is not None else None,
                        fidelity=str(args.get("fidelity", "exact_trade")),
                        execution_mode=execution_mode,
                    ),
                )
                if result.get("ok") is False:
                    raise RuntimeError(result.get("message", "Backtest failed"))
                artifact_payload = result.pop("_artifact_payload", None)
                if artifact_payload is not None:
                    equity_curve = artifact_payload.get("equity_curve", [])
                    trades = artifact_payload.get("trades", [])
                else:
                    equity_curve = result.pop("equity_curve", [])
                    trades = result.pop("trades", [])
                result["artifact_path"] = write_backtest_artifact(run_id, equity_curve, trades)
                result["run_id"] = run_id

                with get_session(get_engine()) as session:
                    row = session.get(BacktestRun, run_id)
                    if row:
                        row.status = "complete"
                        row.results_json = json.dumps(result)
                        row.completed_at = int(datetime.now(tz=UTC).timestamp())
                        session.commit()

                if args.get("include_curve", False):
                    artifact = read_backtest_artifact(run_id)
                    result["equity_curve"] = artifact.get("equity_curve", [])
                    result["trades"] = artifact.get("trades", [])
                return _respond(result)
            except Exception as exc:
                with get_session(get_engine()) as session:
                    row = session.get(BacktestRun, run_id)
                    if row:
                        row.status = "failed"
                        row.error = traceback.format_exc()
                        row.completed_at = int(datetime.now(tz=UTC).timestamp())
                        session.commit()
                return _respond(_error_payload("StrategyError", str(exc), fix="Fix the strategy and retry."))

        if name == "research_markets":
            days = int(args.get("days", 7))
            platform = args.get("platform", "all")
            category = args.get("category")
            tags = args.get("tags")
            market_ids = args.get("market_ids")
            limit = int(args.get("limit", 20))
            sync_limit = int(args.get("sync_limit", 100))
            include_raw = bool(args.get("include_raw", False))

            sync_response = await call_tool(
                "sync_data",
                {
                    "days": days,
                    "platform": platform,
                    "market_ids": market_ids,
                    "limit": sync_limit,
                },
            )
            sync_payload = json.loads(sync_response[0].text)
            if not sync_payload.get("ok"):
                return _respond(
                    _error_payload(
                        "ResearchFailed",
                        "research_markets failed during sync_data",
                        fix=sync_payload.get("fix", "Retry sync_data first, then rerun research_markets."),
                        step="sync_data",
                        sync=sync_payload,
                    )
                )

            markets_response = await call_tool(
                "get_markets",
                {
                    "platform": platform,
                    "category": category,
                    "tags": tags,
                    "limit": limit,
                },
            )
            markets_payload = json.loads(markets_response[0].text)
            if not markets_payload.get("ok"):
                return _respond(
                    _error_payload(
                        "ResearchFailed",
                        "research_markets failed during get_markets",
                        fix=markets_payload.get("fix", "Retry get_markets with broader filters."),
                        step="get_markets",
                        sync=sync_payload,
                        markets=markets_payload,
                    )
                )

            history = []
            history_errors = []
            for market in markets_payload.get("markets", []):
                history_response = await call_tool(
                    "get_history",
                    {
                        "market_id": market["id"],
                        "days": days,
                        "include_raw": include_raw,
                    },
                )
                history_payload = json.loads(history_response[0].text)
                if history_payload.get("ok"):
                    history.append(history_payload)
                else:
                    history_errors.append({"market_id": market["id"], **history_payload})

            payload = {
                "ok": len(history_errors) == 0,
                "sync": sync_payload,
                "markets": markets_payload.get("markets", []),
                "history": history,
                "history_errors": history_errors,
                "count": len(markets_payload.get("markets", [])),
            }
            if history_errors:
                payload["error"] = "PartialHistoryFailure"
                payload["message"] = "History fetch failed for one or more markets."
                payload["fix"] = history_errors[0].get(
                    "fix",
                    "Retry with a smaller limit or sync_data for failed market_ids.",
                )
            return _respond(payload)

        if name == "validate_and_backtest":
            validation = validate_strategy_file(args["strategy_path"])
            if not validation.get("ok") or not validation.get("valid", False):
                return _respond(
                    _error_payload(
                        "StrategyValidationError",
                        "Strategy validation failed",
                        fix="Fix validation errors, then rerun validate_and_backtest.",
                        validation=validation,
                    )
                )
            backtest_response = await call_tool("run_backtest", args)
            backtest_payload = json.loads(backtest_response[0].text)
            if not backtest_payload.get("ok"):
                return _respond(
                    _error_payload(
                        "BacktestFailed",
                        "Backtest step failed after validation",
                        fix=backtest_payload.get("fix", "Retry run_backtest directly for more detail."),
                        validation=validation,
                        backtest=backtest_payload,
                    )
                )
            return _respond({"ok": True, "validation": validation, "backtest": backtest_payload})

        if name == "get_backtest":
            run_id = args["run_id"]
            row = cache.get_backtest_run(run_id)
            if not row:
                return _respond(
                    _error_payload(
                        "NotFound",
                        "run not found",
                        fix=f"Call list_backtests then retry with a valid run_id (missing: {run_id}).",
                    )
                )
            if row.results_json:
                data = json.loads(row.results_json)
                if args.get("include_curve", False):
                    artifact = read_backtest_artifact(run_id)
                    if artifact.get("equity_curve") or "equity_curve" not in data:
                        data["equity_curve"] = artifact.get("equity_curve", [])
                    if artifact.get("trades") or "trades" not in data:
                        data["trades"] = artifact.get("trades", [])
                return _respond(data)
            return _respond({"ok": True, "status": row.status})

        if name == "list_backtests":
            rows = cache.list_backtest_runs(limit=100, lightweight=True)
            return _respond({"ok": True, "runs": [{"id": r.id, "status": r.status, "strategy_path": r.strategy_path} for r in rows]})

        if name == "start_paper_trade":
            strategy_path = Path(args["strategy_path"]).resolve()
            if not strategy_path.exists() or not strategy_path.is_file():
                return _respond(_error_payload("BadRequest", f"Strategy file not found: {args['strategy_path']}"))
            if not str(strategy_path).endswith(".py"):
                return _respond(_error_payload("BadRequest", "Strategy file must be a .py file"))

            # Validate strategy before starting daemon
            validation = validate_strategy_file(str(strategy_path))
            if not validation.get("valid", False):
                return _respond(
                    _error_payload(
                        "StrategyValidationError",
                        "Strategy validation failed",
                        fix="Fix validation errors, then retry start_paper_trade.",
                        validation=validation,
                    )
                )

            portfolio_id = str(uuid.uuid4())
            initial_cash = float(args.get("initial_cash", 10000.0))
            strategy_hash = hashlib.sha256(strategy_path.read_bytes()).hexdigest()
            with get_session(get_engine()) as session:
                session.add(
                    PaperPortfolio(
                        id=portfolio_id,
                        strategy_path=str(strategy_path),
                        strategy_hash=strategy_hash,
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
            return _respond({"ok": True, "portfolio_id": portfolio_id, "pid": pid})

        if name == "get_portfolio":
            portfolio_id = args["portfolio_id"]
            p = cache.get_portfolio(portfolio_id)
            if not p:
                return _respond(
                    _error_payload(
                        "NotFound",
                        "portfolio not found",
                        fix=f"Call list_paper_trades then retry with a valid portfolio_id (missing: {portfolio_id}).",
                    )
                )
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
            return _respond(
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
            portfolio_id = args["portfolio_id"]
            p = cache.get_portfolio(portfolio_id)
            if not p:
                return _respond(
                    _error_payload(
                        "NotFound",
                        "portfolio not found",
                        fix=f"Call list_paper_trades then retry with a valid portfolio_id (missing: {portfolio_id}).",
                    )
                )
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
            return _respond({"ok": True, "portfolio_id": p.id, "stopped": True})

        if name == "list_paper_trades":
            rows = cache.list_paper_portfolios()
            return _respond({"ok": True, "portfolios": [{"id": p.id, "status": p.status, "pid": p.pid} for p in rows]})

        if name == "sync_data":
            client = PmxtClient()
            days = int(args.get("days", 7))
            platform = args.get("platform", "all")
            market_ids = args.get("market_ids")
            limit = int(args.get("limit", 100))
            markets = client.get_markets(platform=platform, market_ids=market_ids, limit=limit)

            start_ts = int(time.time()) - days * 24 * 3600
            end_ts = int(time.time())
            ob_store = OrderBookStore()
            pp = 0
            ob_files = 0
            synced = 0
            errors = []
            for m in markets:
                try:
                    cache.upsert_market(m)
                    candles = client.get_candlesticks(m.condition_id, m.platform, start_ts, end_ts, 60)
                    cache.upsert_price_points_batch(m.id, m.platform.value, candles, source="pmxt", granularity="1h")
                    pp += len(candles)
                    ob = client.get_orderbook_snapshots(m.id, m.platform, start_ts, end_ts, 100)
                    ob_files += ob_store.write(m.platform.value, m.id, ob)
                    cache.mark_market_synced(m.id, int(time.time()))
                    synced += 1
                except Exception as exc:
                    errors.append({"market_id": m.id, "error": str(exc)})

            return _respond(
                {
                    "ok": len(errors) == 0,
                    "markets_synced": synced,
                    "price_points_fetched": pp,
                    "orderbook_files_written": ob_files,
                    "errors": errors,
                }
            )

        return _respond(_error_payload("UnknownTool", f"Unknown tool: {name}"))
    except AgentTraderError as exc:
        return _respond(_error_payload(exc.error, exc.message, fix=exc.fix, **exc.extra))
    except KeyError as exc:
        missing = str(exc).strip("'")
        return _respond(
            _error_payload(
                "BadRequest",
                f"Missing required argument: {missing}",
                fix=f"Call list_tools and retry '{name}' with the required '{missing}' field.",
            )
        )
    except OperationalError as exc:
        err_msg = str(exc)
        if "no such table" in err_msg.lower():
            return _respond(
                _error_payload(
                    "NotInitialized",
                    "Database schema missing. agenttrader is not initialized.",
                    fix="Run: agenttrader init",
                )
            )
        return _respond(_error_payload("OperationalError", err_msg, fix="Retry; if this persists run: agenttrader init"))
    except Exception as exc:  # pragma: no cover
        return _respond(
            _error_payload(
                exc.__class__.__name__,
                str(exc),
                fix="Inspect input arguments and retry. Use list_tools for the expected schema.",
            )
        )


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
