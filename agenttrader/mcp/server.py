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
from agenttrader.data.source_selector import get_best_data_source
from agenttrader.db.health import check_schema
from agenttrader.errors import AgentTraderError
from agenttrader.data.pmxt_client import PmxtClient
from agenttrader.data.orderbook_store import OrderBookStore
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


def _pid_alive(pid: int) -> bool:
    """Check whether a process with the given PID is still running."""
    import os
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False


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
            description="List prediction markets. Uses best available data source (index > parquet > cache). Filter by platform, category, tags.",
            inputSchema={
                "type": "object",
                "properties": {
                    "platform": {"type": "string", "enum": ["polymarket", "kalshi", "all"], "default": "all"},
                    "category": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "market_ids": {"type": "array", "items": {"type": "string"}, "description": "Look up specific markets by ID. Bypasses volume/limit ordering."},
                    "limit": {"type": "integer", "default": 20},
                },
            },
        ),
        types.Tool(name="get_price", description="Get latest price for a market. Uses best available data source (index > cache).", inputSchema={"type": "object", "properties": {"market_id": {"type": "string"}, "platform": {"type": "string", "enum": ["polymarket", "kalshi"], "default": "polymarket"}}, "required": ["market_id"]}),
        types.Tool(
            name="get_history",
            description="Get market history analytics. Uses best available data source (index > parquet > cache). Raw history is omitted by default; set include_raw=true to include points.",
            inputSchema={
                "type": "object",
                "properties": {
                    "market_id": {"type": "string"},
                    "days": {"type": "integer", "default": 7},
                    "platform": {
                        "type": "string",
                        "enum": ["polymarket", "kalshi"],
                        "default": "polymarket",
                        "description": "Platform hint. Required when using indexed/parquet data source.",
                    },
                    "include_raw": {"type": "boolean", "default": False},
                },
                "required": ["market_id"],
            },
        ),
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
        types.Tool(
            name="research_markets",
            description=(
                "Compound research workflow: list filtered markets and return history analytics for each. "
                "Uses best available data source (index > parquet > cache). "
                "Set sync_first=true to sync live data before researching (only relevant for sqlite-cache)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "default": 7},
                    "platform": {"type": "string", "enum": ["polymarket", "kalshi", "all"], "default": "all"},
                    "category": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "market_ids": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer", "default": 20},
                    "sync_first": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "If true, sync fresh data from PMXT before researching. "
                            "Only relevant when using sqlite-cache. Default: false."
                        ),
                    },
                    "sync_limit": {"type": "integer", "default": 100},
                    "include_raw": {"type": "boolean", "default": False},
                },
            },
        ),
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
        types.Tool(
            name="sync_data",
            description=(
                "Sync live market data from PMXT into local SQLite cache. "
                "Use for paper trading or when you need real-time prices. "
                "Not needed for backtesting if the indexed dataset is available."
            ),
            inputSchema={"type": "object", "properties": {"days": {"type": "integer", "default": 7}, "platform": {"type": "string", "default": "all"}, "market_ids": {"type": "array", "items": {"type": "string"}}, "limit": {"type": "integer", "default": 100}, "category": {"type": "string", "description": "Filter markets by category (e.g. politics, crypto, sports)"}, "resolved": {"type": "boolean", "default": False, "description": "Include resolved/closed markets"}, "granularity": {"type": "string", "enum": ["minute", "hourly", "daily"], "default": "hourly", "description": "Candlestick granularity"}}},
        ),
        types.Tool(
            name="debug_data_sources",
            description=(
                "Diagnose data source availability. Returns status of all data backends: "
                "DuckDB index, parquet files, SQLite cache, and schema health. "
                "Use this first when data lookups fail or return unexpected results."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
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
            source, source_name = get_best_data_source()
            platform = args.get("platform", "all")
            market_ids = args.get("market_ids")
            if market_ids and hasattr(source, "get_markets_by_ids"):
                markets = source.get_markets_by_ids(market_ids, platform=platform)
            elif market_ids and source_name == "sqlite-cache":
                markets = []
                for mid in market_ids:
                    m = cache.get_market(mid)
                    if m and (platform == "all" or m.platform.value == platform):
                        markets.append(m)
            else:
                kwargs = {
                    "platform": platform,
                    "category": args.get("category"),
                    "limit": int(args.get("limit", 20)),
                }
                if source_name == "sqlite-cache" and args.get("tags"):
                    kwargs["tags"] = args.get("tags")
                markets = source.get_markets(**kwargs)
            return _respond({
                "ok": True,
                "data_source": source_name,
                "count": len(markets),
                "markets": [
                    m.__dict__ | {"platform": m.platform.value, "market_type": m.market_type.value}
                    for m in markets
                ],
            })

        if name == "get_price":
            market_id = args["market_id"]
            platform = args.get("platform", "polymarket")
            source, source_name = get_best_data_source()
            latest = source.get_latest_price(market_id, platform)
            if not latest:
                return _respond(
                    _error_payload(
                        "MarketNotFound",
                        f"No price data found for {market_id}",
                        fix=f"Call sync_data(market_ids=['{market_id}']) or check market_id is correct.",
                    )
                )
            return _respond({"ok": True, "data_source": source_name, "market_id": market_id, "price": latest.__dict__})

        if name == "get_history":
            market_id = args["market_id"]
            source, source_name = get_best_data_source()

            if source_name == "sqlite-cache":
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

            if source_name == "sqlite-cache":
                history = source.get_price_history(market_id, start_ts, end_ts)
            else:
                platform = args.get("platform", "polymarket")
                history = source.get_price_history(market_id, platform, start_ts, end_ts)

            include_raw = bool(args.get("include_raw", False))
            payload = {
                "ok": True,
                "data_source": source_name,
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

                bt = BacktestEngine()
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

            source, source_name = get_best_data_source()
            sync_payload = None

            # Only sync if explicitly requested AND using sqlite-cache
            if source_name == "sqlite-cache" and args.get("sync_first", False):
                sync_response = await call_tool(
                    "sync_data",
                    {"days": days, "platform": platform, "market_ids": market_ids, "limit": sync_limit},
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

            # Get markets from best source
            if market_ids and hasattr(source, "get_markets_by_ids"):
                # Direct ID lookup — bypasses volume-ordered LIMIT
                markets = source.get_markets_by_ids(market_ids, platform=platform)
            elif market_ids and source_name == "sqlite-cache":
                # SQLite cache: look up each ID directly
                markets = []
                for mid in market_ids:
                    m = cache.get_market(mid)
                    if m and (platform == "all" or m.platform.value == platform):
                        markets.append(m)
            else:
                mkwargs = {"platform": platform, "category": category, "limit": limit}
                if source_name == "sqlite-cache" and tags:
                    mkwargs["tags"] = tags
                markets = source.get_markets(**mkwargs)

            # Get history for each market
            end_ts = int(time.time())
            start_ts = end_ts - days * 24 * 3600
            history = []
            history_errors = []
            for market in markets:
                try:
                    mid = market.id if hasattr(market, "id") else market["id"]
                    plat = market.platform.value if hasattr(market.platform, "value") else str(market.platform)
                    if source_name == "sqlite-cache":
                        pts = source.get_price_history(mid, start_ts, end_ts)
                    else:
                        pts = source.get_price_history(mid, plat, start_ts, end_ts)
                    analytics = _compute_history_analytics(pts, end_ts)
                    entry = {"ok": True, "market_id": mid, "days": days, "analytics": analytics}
                    if include_raw:
                        entry["history"] = [h.__dict__ for h in pts]
                    history.append(entry)
                except Exception as exc:
                    history_errors.append({"market_id": getattr(market, "id", "?"), "error": str(exc)})

            payload = {
                "ok": len(history_errors) == 0,
                "data_source": source_name,
                "markets": [
                    m.__dict__ | {"platform": m.platform.value, "market_type": m.market_type.value}
                    for m in markets
                ],
                "history": history,
                "history_errors": history_errors,
                "count": len(markets),
            }
            if sync_payload is not None:
                payload["sync"] = sync_payload
            if history_errors:
                payload["error"] = "PartialHistoryFailure"
                payload["message"] = "History fetch failed for one or more markets."
                payload["fix"] = "Retry with a smaller limit or sync_data for failed market_ids."
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
            proc = daemon.start_as_daemon()
            pid = proc.pid

            # Health check: wait briefly and verify daemon is still alive
            import time as _time
            _time.sleep(1.0)
            exit_code = proc.poll()
            if exit_code is not None:
                # Daemon died immediately — read stderr log for details
                error_detail = ""
                if hasattr(daemon, "_stderr_path") and daemon._stderr_path.exists():
                    error_detail = daemon._stderr_path.read_text(encoding="utf-8").strip()[-500:]
                if hasattr(daemon, "_stderr_file"):
                    daemon._stderr_file.close()
                with get_session(get_engine()) as session:
                    row = session.get(PaperPortfolio, portfolio_id)
                    if row:
                        row.status = "failed"
                        row.pid = pid
                        row.stopped_at = int(datetime.now(tz=UTC).timestamp())
                        session.commit()
                return _respond(
                    _error_payload(
                        "DaemonCrashed",
                        f"Paper trading daemon exited immediately (exit code {exit_code})",
                        fix="Check daemon log for details. Common cause: SQLite write permissions.",
                        stderr=error_detail or "(no output captured)",
                    )
                )

            if hasattr(daemon, "_stderr_file"):
                daemon._stderr_file.close()
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
            # Auto-correct stale "running" status when daemon PID is dead
            if p.status == "running" and p.pid and not _pid_alive(int(p.pid)):
                with get_session(get_engine()) as session:
                    row = session.get(PaperPortfolio, p.id)
                    if row:
                        row.status = "dead"
                        session.commit()
                p = cache.get_portfolio(portfolio_id)
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
                except (ProcessLookupError, OSError):
                    pass  # Process already dead or Windows WinError 87
            with get_session(get_engine()) as session:
                row = session.get(PaperPortfolio, p.id)
                if row:
                    row.status = "stopped"
                    row.stopped_at = int(datetime.now(tz=UTC).timestamp())
                    session.commit()
            return _respond({"ok": True, "portfolio_id": p.id, "stopped": True})

        if name == "list_paper_trades":
            rows = cache.list_paper_portfolios()
            # Auto-correct stale "running" rows where the daemon PID is dead
            for p in rows:
                if p.status == "running" and p.pid and not _pid_alive(int(p.pid)):
                    with get_session(get_engine()) as session:
                        row = session.get(PaperPortfolio, p.id)
                        if row:
                            row.status = "dead"
                            session.commit()
                    p.status = "dead"
            return _respond({"ok": True, "portfolios": [{"id": p.id, "status": p.status, "pid": p.pid} for p in rows]})

        if name == "sync_data":
            client = PmxtClient()
            days = int(args.get("days", 7))
            platform = args.get("platform", "all")
            market_ids = args.get("market_ids")
            limit = int(args.get("limit", 100))
            category = args.get("category")
            resolved = args.get("resolved", False)
            granularity = args.get("granularity", "hourly")
            granularity_map = {"minute": 1, "hourly": 60, "daily": 1440}
            interval_minutes = granularity_map.get(granularity, 60)
            mk = {"platform": platform, "market_ids": market_ids, "limit": limit}
            if category:
                mk["category"] = category
            if resolved:
                mk["resolved"] = resolved
            markets = client.get_markets(**mk)

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
                    candles = client.get_candlesticks(m.condition_id, m.platform, start_ts, end_ts, interval_minutes)
                    gran_label = {"minute": "1m", "hourly": "1h", "daily": "1d"}.get(granularity, "1h")
                    cache.upsert_price_points_batch(m.id, m.platform.value, candles, source="pmxt", granularity=gran_label)
                    pp += len(candles)
                    ob = client.get_orderbook_snapshots(m.id, m.platform, start_ts, end_ts, 100)
                    ob_files += ob_store.write(m.platform.value, m.id, ob)
                    cache.mark_market_synced(m.id, int(time.time()))
                    synced += 1
                except Exception as exc:
                    errors.append({"market_id": m.id, "error": str(exc)})

            if market_ids and synced == 0 and len(errors) == 0:
                return _respond({
                    "ok": False,
                    "error": "NoMarketsFound",
                    "message": f"None of the {len(market_ids)} requested market_ids were found.",
                    "fix": "Check market_ids are correct and PMXT credentials are configured.",
                })

            return _respond(
                {
                    "ok": len(errors) == 0,
                    "markets_synced": synced,
                    "price_points_fetched": pp,
                    "orderbook_files_written": ob_files,
                    "errors": errors,
                }
            )

        if name == "debug_data_sources":
            base = Path.home() / ".agenttrader"
            diag = {"ok": True, "sources": {}}

            # 1. DuckDB normalized index
            index_path = base / "backtest_index.duckdb"
            if index_path.exists():
                try:
                    from agenttrader.data.index_adapter import BacktestIndexAdapter
                    adapter = BacktestIndexAdapter()
                    if adapter.is_available():
                        diag["sources"]["normalized_index"] = {
                            "available": True,
                            "path": str(index_path),
                            "size_mb": round(index_path.stat().st_size / 1e6, 1),
                        }
                    else:
                        diag["sources"]["normalized_index"] = {
                            "available": False, "path": str(index_path),
                            "reason": "File exists but tables not readable",
                            "fix": "Run: agenttrader dataset build-index --force",
                        }
                except Exception as e:
                    diag["sources"]["normalized_index"] = {
                        "available": False, "error": str(e),
                        "fix": "Run: agenttrader dataset build-index --force",
                    }
            else:
                diag["sources"]["normalized_index"] = {
                    "available": False, "reason": "File not found",
                    "fix": "Run: agenttrader dataset download && agenttrader dataset build-index",
                }

            # 2. Raw parquet files
            data_dir = base / "data"
            parquet_status = {}
            for plat in ["polymarket", "kalshi"]:
                for subdir in ["markets", "trades"]:
                    p = data_dir / plat / subdir
                    if p.exists():
                        parquet_status[f"{plat}/{subdir}"] = len(list(p.glob("*.parquet")))
                    else:
                        parquet_status[f"{plat}/{subdir}"] = 0
            total_parquet = sum(parquet_status.values())
            diag["sources"]["raw_parquet"] = {
                "available": total_parquet > 0,
                "data_dir": str(data_dir),
                "file_counts": parquet_status,
            }
            if total_parquet == 0:
                diag["sources"]["raw_parquet"]["fix"] = "Run: agenttrader dataset download"

            # 3. SQLite cache
            db_path = base / "db.sqlite"
            schema_health = check_schema(db_path)
            if db_path.exists():
                try:
                    import sqlite3
                    conn = sqlite3.connect(str(db_path))
                    market_count = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
                    price_count = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
                    conn.close()
                    diag["sources"]["sqlite_cache"] = {
                        "available": True,
                        "path": str(db_path),
                        "schema_ok": schema_health["ok"],
                        "schema_issues": schema_health.get("missing_columns", []),
                        "markets_cached": market_count,
                        "price_points": price_count,
                    }
                    if not schema_health["ok"]:
                        diag["sources"]["sqlite_cache"]["fix"] = schema_health.get("fix")
                except Exception as e:
                    diag["sources"]["sqlite_cache"] = {
                        "available": False, "error": str(e),
                        "fix": "Run: agenttrader init",
                    }
            else:
                diag["sources"]["sqlite_cache"] = {
                    "available": False, "reason": "Database not found",
                    "fix": "Run: agenttrader init",
                }

            # 4. Active data source
            _, active_name = get_best_data_source()
            diag["active_data_source"] = active_name

            return _respond(diag)

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
    from agenttrader.config import DB_PATH

    if DB_PATH.exists():
        health = check_schema(DB_PATH)
        if not health["ok"]:
            print(f"ERROR: {health['error']}", file=sys.stderr)
            if "missing_columns" in health:
                print(f"Missing: {', '.join(health['missing_columns'])}", file=sys.stderr)
            print(f"Fix: {health['fix']}", file=sys.stderr)
            sys.exit(1)

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
