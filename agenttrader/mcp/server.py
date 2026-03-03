# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import shutil
import signal
import statistics
import subprocess
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
from agenttrader.config import BACKTEST_INDEX_PATH, DB_PATH, SHARED_DATA_DIR, is_initialized, load_config
from agenttrader.core.backtest_engine import BacktestConfig, BacktestEngine
from agenttrader.core.base_strategy import BaseStrategy
from agenttrader.core.context import LiveContext
from agenttrader.core.paper_daemon import PaperDaemon, read_runtime_status, runtime_status_path
from agenttrader.data.backtest_artifacts import read_backtest_artifact, write_backtest_artifact
from agenttrader.data.cache import DataCache
from agenttrader.data.models import ExecutionMode, PricePoint
from agenttrader.data.index_adapter import BacktestIndexAdapter
from agenttrader.data.source_selector import get_best_data_source, get_all_sources, invalidate_source_cache
from agenttrader.db.health import check_schema
from agenttrader.errors import AgentTraderError
from agenttrader.data.pmxt_client import PmxtClient
from agenttrader.data.orderbook_store import OrderBookStore
from agenttrader.db import get_engine, get_session
from agenttrader.db.schema import BacktestRun, PaperPortfolio
from agenttrader.perf_logging import log_performance_event


server = Server("agenttrader")
MCP_SESSION_ID = f"mcp-{uuid.uuid4().hex[:12]}"
PMXT_SIDECAR_PATH_FRAGMENT = "pmxt/_server/server/bundled.js"
PMXT_GUARDED_TOOLS = {"match_markets", "start_paper_trade", "sync_data"}


def _text(obj) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(obj, default=str))]


def _bounded_int(args: dict, key: str, default: int, lo: int, hi: int) -> int:
    """Extract an integer parameter clamped to [lo, hi]."""
    return max(lo, min(hi, int(args.get(key, default))))


def _bounded_float(args: dict, key: str, default: float, lo: float, hi: float) -> float:
    """Extract a float parameter clamped to [lo, hi]."""
    return max(lo, min(hi, float(args.get(key, default))))


_cached_index_adapter: BacktestIndexAdapter | None = None
_cached_index_checked = False


def _get_cached_index_adapter() -> BacktestIndexAdapter | None:
    """Return a cached BacktestIndexAdapter if the index is available, else None."""
    global _cached_index_adapter, _cached_index_checked
    if _cached_index_checked:
        return _cached_index_adapter
    _cached_index_checked = True
    try:
        adapter = BacktestIndexAdapter()
        if adapter.is_available():
            _cached_index_adapter = adapter
        else:
            adapter.close()
    except Exception:
        pass
    return _cached_index_adapter


DEFAULT_FIXES = {
    "NotInitialized": "Run: agenttrader init",
    "UnknownTool": "Call list_tools to see supported tool names, then retry.",
    "BadRequest": "Check required tool arguments and retry.",
    "MarketNotCached": "Call sync_data with market_ids=['<market_id>'] (or sync the platform), then retry.",
    "NotFound": "Verify the id exists first (list_backtests or list_paper_portfolios), then retry.",
    "PmxtSidecarConflict": "Stop duplicate PMXT sidecar processes so only zero or one is running, then retry.",
    "InternalError": "This is a server bug, not a strategy issue. Report it or retry.",
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
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False


def _normalize_process_command_line(command_line: str | None) -> str:
    return str(command_line or "").replace("\\", "/").lower()


def _is_pmxt_sidecar_process(command_line: str | None) -> bool:
    return PMXT_SIDECAR_PATH_FRAGMENT in _normalize_process_command_line(command_line)


def _list_process_command_lines() -> list[dict[str, str | int]]:
    if os.name == "nt":
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            return []
        script = (
            "$ErrorActionPreference='Stop'; "
            "Get-CimInstance Win32_Process | "
            "Select-Object ProcessId, CommandLine | "
            "ForEach-Object { "
            "if ($null -ne $_.CommandLine) { "
            "[pscustomobject]@{ pid = [int]$_.ProcessId; command_line = [string]$_.CommandLine } "
            "} "
            "} | ConvertTo-Json -Compress"
        )
        try:
            proc = subprocess.run(
                [powershell, "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return []
        rows = payload if isinstance(payload, list) else [payload]
        return [
            {"pid": int(row.get("pid")), "command_line": str(row.get("command_line", ""))}
            for row in rows
            if row and row.get("pid") is not None
        ]

    try:
        proc = subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []

    rows: list[dict[str, str | int]] = []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2:
            continue
        pid_text, command_line = parts
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        rows.append({"pid": pid, "command_line": command_line})
    return rows


def _detect_pmxt_sidecars() -> list[dict[str, str | int]]:
    return [
        {
            "pid": int(row["pid"]),
            "command_line": str(row.get("command_line", "")),
        }
        for row in _list_process_command_lines()
        if _is_pmxt_sidecar_process(row.get("command_line"))
    ]


def _build_pmxt_sidecar_conflict_payload(sidecars: list[dict[str, str | int]]) -> dict | None:
    if len(sidecars) <= 1:
        return None

    ordered = sorted(
        (
            {
                "pid": int(sidecar.get("pid", 0)),
                "command_line": str(sidecar.get("command_line", "")),
            }
            for sidecar in sidecars
        ),
        key=lambda item: item["pid"],
    )
    return _error_payload(
        "PmxtSidecarConflict",
        (
            "Multiple PMXT sidecar processes are running. This is unsupported because the PMXT Python SDK can "
            "attach to the wrong healthy sidecar and produce incorrect port + access-token pairing."
        ),
        fix="Stop duplicate PMXT sidecar processes (node ... pmxt/_server/server/bundled.js) so only zero or one remains, then retry.",
        sidecars=ordered,
        sidecar_count=len(ordered),
    )


def _pmxt_sidecar_conflict_payload() -> dict | None:
    return _build_pmxt_sidecar_conflict_payload(_detect_pmxt_sidecars())


def _ensure_pmxt_sidecar_safe() -> None:
    conflict = _pmxt_sidecar_conflict_payload()
    if conflict is None:
        return

    print(f"ERROR: {conflict['message']}", file=sys.stderr)
    print(
        "Multiple PMXT sidecars can cause PMXT to bind the wrong port/access-token pair, which surfaces "
        "misleading auth failures like 'Unauthorized: Invalid or missing access token'.",
        file=sys.stderr,
    )
    for sidecar in conflict.get("sidecars", []):
        print(
            f"PMXT sidecar pid={sidecar['pid']}: {sidecar['command_line']}",
            file=sys.stderr,
        )
    print(f"Fix: {conflict['fix']}", file=sys.stderr)
    raise SystemExit(1)


def _persist_backtest_progress(run_id: str, payload: dict) -> None:
    with get_session(get_engine()) as session:
        row = session.get(BacktestRun, run_id)
        if row:
            row.results_json = json.dumps(payload)
            session.commit()


def _make_mcp_backtest_progress_callback(run_id: str):
    state = {"ok": True, "run_id": run_id, "status": "running"}

    def _callback(event: dict) -> None:
        kind = event.get("kind")
        if kind == "preflight":
            state["preflight"] = dict(event)
        elif kind == "progress":
            state["progress"] = dict(event)
        _persist_backtest_progress(run_id, state)

    return _callback


def _extract_backtest_progress(results_json: str | None) -> dict | None:
    if not results_json:
        return None
    try:
        payload = json.loads(results_json)
    except json.JSONDecodeError:
        return None
    progress = payload.get("progress")
    if not isinstance(progress, dict):
        return None
    return progress


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
            "last_point_timestamp": None,
            "hours_since_last_point": None,
            "has_24h_reference": False,
        }

    prices = [float(h.yes_price) for h in history]
    current_price = prices[-1]
    avg_7d = sum(prices) / len(prices)
    last_point_ts = int(history[-1].timestamp)
    cutoff_24h = end_ts - 24 * 3600
    recent_24h = [h for h in history if int(h.timestamp) >= cutoff_24h]
    has_24h_reference = bool(recent_24h)
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
        "last_point_timestamp": last_point_ts,
        "hours_since_last_point": max(0.0, (end_ts - last_point_ts) / 3600.0),
        "has_24h_reference": has_24h_reference,
    }


def _compute_capabilities(markets: list, cache) -> dict[str, dict]:
    """Build per-market capability annotations from local sources only."""
    if not markets:
        return {}

    ids = [getattr(m, "id", None) or m.get("id") for m in markets]
    ids = [mid for mid in ids if mid]

    # Backtest index ranges (single DuckDB query, reuse cached adapter)
    index_ranges: dict[str, tuple[int, int]] = {}
    try:
        adapter = _get_cached_index_adapter()
        if adapter is not None:
            index_ranges = adapter.get_market_date_ranges(ids)
    except Exception:
        pass

    # Cache availability (per-market SQLite lookups)
    cache_info: dict[str, object] = {}
    for mid in ids:
        try:
            latest = cache.get_latest_price(mid)
            if latest is not None:
                cache_info[mid] = latest
        except Exception:
            pass

    caps: dict[str, dict] = {}
    for m in markets:
        mid = getattr(m, "id", None) or m.get("id")
        if not mid:
            continue

        # Backtest
        if mid in index_ranges:
            min_ts, max_ts = index_ranges[mid]
            backtest = {
                "index_available": True,
                "index_start": datetime.fromtimestamp(min_ts, tz=UTC).strftime("%Y-%m-%d"),
                "index_end": datetime.fromtimestamp(max_ts, tz=UTC).strftime("%Y-%m-%d"),
            }
        else:
            backtest = {"index_available": False, "index_start": None, "index_end": None}

        # History cache
        if mid in cache_info:
            lp = cache_info[mid]
            history = {
                "cache_available": True,
                "last_point_timestamp": datetime.fromtimestamp(
                    int(lp.timestamp), tz=UTC
                ).isoformat(),
            }
        else:
            history = {"cache_available": False, "last_point_timestamp": None}

        # Sync
        resolved = bool(getattr(m, "resolved", False))
        sync = {"can_attempt_live_sync": not resolved}

        caps[mid] = {"backtest": backtest, "history": history, "sync": sync}

    return caps


def _is_market_resolved(market) -> bool:
    return bool(getattr(market, "resolved", False))


def _normalize_runtime_status_for_portfolio(portfolio, runtime_status: dict | None) -> dict | None:
    status = str(getattr(portfolio, "status", "") or "")
    if status == "running":
        return runtime_status

    normalized = dict(runtime_status or {})
    if not normalized:
        updated_at = getattr(portfolio, "stopped_at", None) or int(time.time())
        normalized = {
            "portfolio_id": getattr(portfolio, "id", None),
            "updated_at": updated_at,
        }

    normalized["portfolio_id"] = getattr(portfolio, "id", normalized.get("portfolio_id"))
    normalized["state"] = status or normalized.get("state")

    if status == "stopped":
        normalized["markets"] = []
        normalized["markets_with_live_price"] = 0
        normalized["markets_degraded"] = 0
        normalized["last_live_update"] = None
    elif status in {"dead", "failed"}:
        normalized["markets_with_live_price"] = 0

    return normalized


def _status_is_active(status: str | None) -> bool:
    return str(status or "").lower() in {"running", "starting"}


def _iso_or_none(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(int(timestamp), tz=UTC).isoformat()


def _normalized_portfolio_pid(portfolio) -> int | None:
    status = str(getattr(portfolio, "status", "") or "").lower()
    if status in {"stopped", "dead", "failed"}:
        return None
    pid = getattr(portfolio, "pid", None)
    return int(pid) if pid is not None else None


def _load_source_provenance(source, source_name: str, market_id: str, platform: str, start_ts: int | None = None, end_ts: int | None = None) -> dict:
    if hasattr(source, "get_provenance"):
        try:
            provenance = source.get_provenance(market_id, platform, start_ts=start_ts, end_ts=end_ts)
        except TypeError:
            provenance = source.get_provenance(market_id, platform)
        return {
            "source": str(getattr(provenance, "source", source_name)),
            "observed": bool(getattr(provenance, "observed", True)),
            "granularity": str(getattr(provenance, "granularity", "unknown")),
        }

    if source_name == "normalized-index":
        return {"source": "index", "observed": True, "granularity": "trade"}
    if source_name == "raw-parquet":
        return {"source": "parquet", "observed": True, "granularity": "trade"}
    if source_name == "sqlite-cache":
        return {"source": "pmxt", "observed": True, "granularity": "1h"}
    return {"source": source_name, "observed": True, "granularity": "unknown"}


def _cache_get_latest_price(cache, market_id: str, platform: str | None = None):
    try:
        if platform is not None:
            return cache.get_latest_price(market_id, platform=platform)
        return cache.get_latest_price(market_id)
    except TypeError:
        return cache.get_latest_price(market_id)
    except AttributeError:
        return None


def _cache_get_price_history(cache, market_id: str, start_ts: int, end_ts: int, platform: str | None = None):
    try:
        if platform is not None:
            return cache.get_price_history(market_id, start_ts, end_ts, platform=platform)
        return cache.get_price_history(market_id, start_ts, end_ts)
    except TypeError:
        return cache.get_price_history(market_id, start_ts, end_ts)
    except AttributeError:
        return []


def _build_candidate_source(
    source,
    source_name: str,
    market_id: str,
    platform: str,
    *,
    latest_ts: int | None,
    had_data: bool,
    start_ts: int | None = None,
    end_ts: int | None = None,
    error: str | None = None,
) -> tuple[dict, dict]:
    provenance = _load_source_provenance(source, source_name, market_id, platform, start_ts=start_ts, end_ts=end_ts)
    entry = {
        "source_name": source_name,
        "source": provenance["source"],
        "observed": provenance["observed"],
        "granularity": provenance["granularity"],
        "had_data": had_data,
        "latest_timestamp": latest_ts,
        "latest_timestamp_iso": _iso_or_none(latest_ts),
        "selected": False,
    }
    if error:
        entry["error"] = error
    return entry, provenance


def _selection_provenance_payload(
    *,
    selected_source: str,
    provenance: dict,
    candidate_sources: list[dict],
    selection_reason: str,
    window_start_ts: int | None = None,
    window_end_ts: int | None = None,
    selected_point_timestamp: int | None = None,
) -> dict:
    now_ts = int(time.time())
    payload = {
        "selected_source": selected_source,
        "source": provenance["source"],
        "observed": provenance["observed"],
        "granularity": provenance["granularity"],
        "selection_reason": selection_reason,
        "generated_at_ts": now_ts,
        "generated_at_iso": _iso_or_none(now_ts),
        "candidate_sources": candidate_sources,
    }
    if window_start_ts is not None:
        payload["window_start_ts"] = int(window_start_ts)
    if window_end_ts is not None:
        payload["window_end_ts"] = int(window_end_ts)
    if selected_point_timestamp is not None:
        payload["selected_point_timestamp"] = int(selected_point_timestamp)
        payload["selected_point_timestamp_iso"] = _iso_or_none(int(selected_point_timestamp))
    return payload


def _select_position_mark_price(side: str, yes_price: float | None, no_price: float | None = None) -> float | None:
    if yes_price is None and no_price is None:
        return None
    normalized_side = str(side or "yes").strip().lower()
    if normalized_side == "no":
        if no_price is not None:
            return float(no_price)
        if yes_price is not None:
            return max(0.0, min(1.0, 1.0 - float(yes_price)))
    if yes_price is not None:
        return float(yes_price)
    return max(0.0, min(1.0, 1.0 - float(no_price)))


def _build_portfolio_live_price_map(portfolio, runtime_status: dict | None) -> dict[str, float]:
    normalized = _normalize_runtime_status_for_portfolio(portfolio, runtime_status) or {}
    live_price_map = {}
    for item in normalized.get("markets", []):
        market_id = item.get("market_id")
        current_price = item.get("current_price")
        if market_id and current_price is not None:
            live_price_map[str(market_id)] = float(current_price)
    return live_price_map


def _build_flatten_context(portfolio, cache: DataCache) -> tuple[LiveContext, dict[str, float]]:
    runtime_status = read_runtime_status(portfolio.id)
    live_price_map = _build_portfolio_live_price_map(portfolio, runtime_status)
    context = LiveContext(
        portfolio.id,
        float(portfolio.initial_cash),
        cache,
        OrderBookStore(),
        pmxt_client=None,
    )
    context._cash = float(portfolio.cash_balance)
    context.load_positions_from_db()

    for pos in context._positions.values():
        live_yes_price = live_price_map.get(pos.market_id)
        if live_yes_price is not None:
            seeded_price = _select_position_mark_price(pos.side, live_yes_price)
        else:
            latest = _cache_get_latest_price(cache, pos.market_id, platform=pos.platform.value)
            seeded_price = None if latest is None else _select_position_mark_price(
                pos.side,
                getattr(latest, "yes_price", None),
                getattr(latest, "no_price", None),
            )
        if seeded_price is not None:
            context.set_live_price(pos.market_id, seeded_price)

    return context, live_price_map


def _compute_portfolio_value(portfolio, positions: list, cache: DataCache, live_price_map: dict[str, float] | None = None) -> float:
    live_yes_prices = live_price_map or {}
    total_value = float(getattr(portfolio, "cash_balance", 0.0) or 0.0)
    for pos in positions:
        live_yes_price = live_yes_prices.get(pos.market_id)
        latest = _cache_get_latest_price(cache, pos.market_id, platform=pos.platform)
        current = _select_position_mark_price(pos.side, live_yes_price) if live_yes_price is not None else None
        if current is None and latest is not None:
            current = _select_position_mark_price(
                pos.side,
                getattr(latest, "yes_price", None),
                getattr(latest, "no_price", None),
            )
        if current is None:
            current = float(pos.avg_cost)
        total_value += float(pos.contracts) * float(current)
    return total_value


def _flatten_portfolio_positions(portfolio, cache: DataCache, *, best_effort: bool) -> dict:
    context, live_price_map = _build_flatten_context(portfolio, cache)
    starting_positions = list(cache.get_open_positions(portfolio.id))
    results = []
    positions_failed = 0
    positions_closed = 0
    realized_pnl = 0.0

    for pos in starting_positions:
        contracts_before = float(pos.contracts)
        try:
            trade_id = context.sell(pos.market_id)
            positions_closed += 1
            trade_row = next((trade for trade in cache.get_trades(portfolio.id, limit=10) if trade.id == trade_id), None)
            pnl = float(trade_row.pnl or 0.0) if trade_row is not None and trade_row.pnl is not None else None
            if pnl is not None:
                realized_pnl += pnl
            results.append(
                {
                    "market_id": pos.market_id,
                    "side": pos.side,
                    "contracts": contracts_before,
                    "trade_id": trade_id,
                    "status": "closed",
                    "realized_pnl": pnl,
                }
            )
        except Exception as exc:
            positions_failed += 1
            results.append(
                {
                    "market_id": pos.market_id,
                    "side": pos.side,
                    "contracts": contracts_before,
                    "status": "failed",
                    "error": str(exc),
                }
            )
            if not best_effort:
                break

    portfolio_row = cache.get_portfolio(portfolio.id)
    remaining_positions = cache.get_open_positions(portfolio.id)
    return {
        "ok": positions_failed == 0,
        "results": results,
        "positions_attempted": len(results),
        "positions_closed": positions_closed,
        "positions_failed": positions_failed,
        "realized_pnl": round(realized_pnl, 6),
        "cash_balance": float(getattr(portfolio_row, "cash_balance", portfolio.cash_balance)),
        "portfolio_value_after_flatten": _compute_portfolio_value(
            portfolio_row or portfolio,
            remaining_positions,
            cache,
            live_price_map=live_price_map,
        ),
        "remaining_positions": len(remaining_positions),
    }


def _detect_orientation_mismatch(points: list, reference_yes_price: float | None) -> dict | None:
    if not points or reference_yes_price is None:
        return None
    recent_prices = [float(getattr(point, "yes_price", 0.0)) for point in points[-min(len(points), 3):]]
    latest_yes = statistics.median(recent_prices)
    reference_yes = float(reference_yes_price)
    direct_delta = abs(latest_yes - reference_yes)
    complement_yes = 1.0 - latest_yes
    complement_delta = abs(complement_yes - reference_yes)
    if direct_delta - complement_delta < 0.2 or complement_delta > 0.12:
        return None
    return {
        "latest_yes_price": round(float(getattr(points[-1], "yes_price", 0.0)), 6),
        "recent_median_yes_price": round(latest_yes, 6),
        "reference_yes_price": round(reference_yes, 6),
        "direct_delta": round(direct_delta, 6),
        "complement_delta": round(complement_delta, 6),
    }


def _invert_price_point(point) -> PricePoint:
    inverted_yes = max(0.0, min(1.0, 1.0 - float(getattr(point, "yes_price", 0.0))))
    return PricePoint(
        timestamp=int(getattr(point, "timestamp", 0) or 0),
        yes_price=inverted_yes,
        no_price=max(0.0, min(1.0, 1.0 - inverted_yes)),
        volume=float(getattr(point, "volume", 0.0) or 0.0),
    )


def _copy_price_point(point) -> PricePoint:
    return PricePoint(
        timestamp=int(getattr(point, "timestamp", 0) or 0),
        yes_price=float(getattr(point, "yes_price", 0.0)),
        no_price=(
            float(getattr(point, "no_price", None))
            if getattr(point, "no_price", None) is not None
            else max(0.0, min(1.0, 1.0 - float(getattr(point, "yes_price", 0.0))))
        ),
        volume=float(getattr(point, "volume", 0.0) or 0.0),
    )


def _local_repair_baseline(points: list[PricePoint], index: int, reference_yes_price: float | None) -> float | None:
    anchors = []
    for offset in (1, 2):
        prev_idx = index - offset
        next_idx = index + offset
        if prev_idx >= 0:
            anchors.append(float(points[prev_idx].yes_price))
        if next_idx < len(points):
            anchors.append(float(points[next_idx].yes_price))
    if reference_yes_price is not None:
        anchors.append(float(reference_yes_price))
    if len(anchors) < 2:
        return None
    if max(anchors) - min(anchors) > 0.18:
        return None
    return float(statistics.median(anchors))


def _find_localized_inverted_candles(points: list[PricePoint], reference_yes_price: float | None) -> list[dict]:
    issues = []
    for index, point in enumerate(points):
        baseline = _local_repair_baseline(points, index, reference_yes_price)
        if baseline is None:
            continue
        current_yes = float(point.yes_price)
        inverted_yes = 1.0 - current_yes
        direct_delta = abs(current_yes - baseline)
        complement_delta = abs(inverted_yes - baseline)
        if direct_delta < 0.25:
            continue
        if complement_delta > 0.12:
            continue
        if direct_delta - complement_delta < 0.18:
            continue
        issues.append(
            {
                "index": index,
                "timestamp": int(point.timestamp),
                "baseline_yes_price": round(baseline, 6),
                "current_yes_price": round(current_yes, 6),
                "repaired_yes_price": round(inverted_yes, 6),
                "direct_delta": round(direct_delta, 6),
                "complement_delta": round(complement_delta, 6),
            }
        )
    return issues


def _normalize_pmxt_candles_to_yes_space(
    points: list,
    *,
    outcome_side: str | None,
    reference_yes_price: float | None,
) -> dict:
    normalized = [_copy_price_point(point) for point in points]
    normalized_outcome_side = str(outcome_side or "").strip().lower() or None
    batch_inverted = False
    inversion_reason = None

    if normalized_outcome_side == "no":
        normalized = [_invert_price_point(point) for point in normalized]
        batch_inverted = True
        inversion_reason = "outcome_side=no"
    elif normalized_outcome_side not in {None, "yes"}:
        inversion_reason = f"outcome_side={normalized_outcome_side}"

    batch_mismatch = None
    if not batch_inverted:
        batch_mismatch = _detect_orientation_mismatch(normalized, reference_yes_price)
        if batch_mismatch is not None:
            normalized = [_invert_price_point(point) for point in normalized]
            batch_inverted = True
            inversion_reason = "reference_complement"

    repairs = []
    for _ in range(2):
        issues = _find_localized_inverted_candles(normalized, reference_yes_price)
        if not issues:
            break
        for issue in issues:
            normalized[issue["index"]] = _invert_price_point(normalized[issue["index"]])
        repairs.extend(issues)

    residual_issues = _find_localized_inverted_candles(normalized, reference_yes_price)
    repair_count = len({issue["timestamp"] for issue in repairs})
    if residual_issues:
        return {
            "ok": False,
            "points": normalized,
            "batch_inverted": batch_inverted,
            "inversion_reason": inversion_reason,
            "batch_mismatch": batch_mismatch,
            "repairs": repairs,
            "residual_issues": residual_issues,
            "message": "Detected localized inverted candles that could not be repaired safely.",
        }
    if repair_count > max(5, max(len(normalized) // 10, 1)):
        return {
            "ok": False,
            "points": normalized,
            "batch_inverted": batch_inverted,
            "inversion_reason": inversion_reason,
            "batch_mismatch": batch_mismatch,
            "repairs": repairs,
            "residual_issues": [],
            "message": "Too many localized inverted candles were detected to repair safely.",
        }

    return {
        "ok": True,
        "points": normalized,
        "batch_inverted": batch_inverted,
        "inversion_reason": inversion_reason,
        "batch_mismatch": batch_mismatch,
        "repairs": repairs,
        "residual_issues": [],
    }


def _replace_pmxt_history_window(
    cache,
    market_id: str,
    platform: str,
    start_ts: int,
    end_ts: int,
    points: list[PricePoint],
    *,
    granularity: str,
) -> None:
    if hasattr(cache, "replace_price_points_window"):
        cache.replace_price_points_window(
            market_id,
            platform,
            start_ts,
            end_ts,
            points,
            source="pmxt",
            granularity=granularity,
        )
        return
    cache.upsert_price_points_batch(market_id, platform, points, source="pmxt", granularity=granularity)


def _pmxt_window_bounds(start_ts: int, end_ts: int, interval_minutes: int) -> tuple[int, int]:
    step_seconds = max(60, int(interval_minutes) * 60)
    aligned_start = (int(start_ts) // step_seconds) * step_seconds
    aligned_end = ((int(end_ts) + step_seconds - 1) // step_seconds) * step_seconds
    return aligned_start, aligned_end


def _get_research_markets(source, source_name: str, *, platform: str, category: str | None, tags: list[str] | None, limit: int, market_ids: list[str] | None, active_only: bool):
    if market_ids and hasattr(source, "get_markets_by_ids"):
        return source.get_markets_by_ids(market_ids, platform=platform)
    if market_ids and source_name == "sqlite-cache":
        markets = []
        for mid in market_ids:
            m = source.get_market(mid)
            if m and (platform == "all" or m.platform.value == platform):
                markets.append(m)
        return markets

    mkwargs = {"platform": platform, "category": category, "limit": max(int(limit), 1)}
    if source_name == "sqlite-cache" and tags:
        mkwargs["tags"] = tags

    fetch_limit = mkwargs["limit"]
    while True:
        request_kwargs = dict(mkwargs)
        request_kwargs["limit"] = fetch_limit
        if active_only:
            request_kwargs["active_only"] = True
        try:
            markets = source.get_markets(**request_kwargs)
        except TypeError:
            request_kwargs.pop("active_only", None)
            markets = source.get_markets(**request_kwargs)

        if not active_only:
            return markets[:limit]

        active_markets = [m for m in markets if not _is_market_resolved(m)]
        if len(active_markets) >= limit or fetch_limit >= 1000 or len(markets) < fetch_limit:
            return active_markets[:limit]
        fetch_limit = min(fetch_limit * 5, 1000)


def _market_platform_value(market) -> str:
    platform = getattr(market, "platform", None)
    if hasattr(platform, "value"):
        return str(platform.value)
    return str(platform or "")


def _market_matches_platform(market, platform: str) -> bool:
    return platform == "all" or _market_platform_value(market) == platform


def _market_matches_category(market, category: str | None) -> bool:
    if not category:
        return True
    wanted = str(category).strip().lower()
    if not wanted:
        return True
    market_category = str(getattr(market, "category", "") or "").lower()
    if market_category == wanted:
        return True
    market_tags = {str(tag).lower() for tag in (getattr(market, "tags", None) or [])}
    return wanted in market_tags


def _market_identifier_aliases(market) -> set[str]:
    aliases = set()
    for value in (getattr(market, "id", None), getattr(market, "condition_id", None)):
        if value is not None and str(value).strip():
            aliases.add(str(value).strip().lower())
    return aliases


def _resolve_market_ids_for_sync(market_ids: list[str] | None, *, platform: str, category: str | None, include_resolved: bool) -> tuple[list, set[str]]:
    requested_ids = [str(market_id).strip() for market_id in (market_ids or []) if str(market_id).strip()]
    if not requested_ids:
        return [], set()

    matched_by_request: dict[str, object] = {}
    matched_request_ids: set[str] = set()
    for source, source_name in get_all_sources():
        try:
            if hasattr(source, "get_markets_by_ids"):
                candidates = source.get_markets_by_ids(requested_ids, platform=platform)
            elif source_name == "sqlite-cache":
                candidates = []
                for market_id in requested_ids:
                    market = source.get_market(market_id)
                    if market is not None:
                        candidates.append(market)
            else:
                continue
        except Exception:
            continue

        for market in candidates:
            if not _market_matches_platform(market, platform):
                continue
            if not include_resolved and _is_market_resolved(market):
                continue
            if not _market_matches_category(market, category):
                continue

            aliases = _market_identifier_aliases(market)
            for requested_id in requested_ids:
                normalized_requested_id = requested_id.lower()
                if normalized_requested_id not in aliases:
                    continue
                matched_request_ids.add(normalized_requested_id)
                matched_by_request.setdefault(requested_id, market)

        if len(matched_request_ids) == len({market_id.lower() for market_id in requested_ids}):
            break

    ordered_markets = []
    seen_market_keys: set[tuple[str, str]] = set()
    for requested_id in requested_ids:
        market = matched_by_request.get(requested_id)
        if market is None:
            continue
        market_key = (_market_platform_value(market), str(getattr(market, "id", "")))
        if market_key in seen_market_keys:
            continue
        seen_market_keys.add(market_key)
        ordered_markets.append(market)

    return ordered_markets, matched_request_ids


def _candlestick_market_id(market) -> str:
    # Always use market.id (the token/outcome ID) for OHLCV fetches.
    # market.condition_id differs between sources (PmxtClient sets it to the
    # outcome_id, while parquet uses the real Polymarket condition_id) and pmxt's
    # fetch_ohlcv expects the token ID, not the event-level condition_id.
    return str(getattr(market, "id"))


def _load_latest_price_from_source(source, source_name: str, market_id: str, platform: str):
    if source_name == "sqlite-cache":
        return _cache_get_latest_price(source, market_id, platform=platform)
    return source.get_latest_price(market_id, platform)


def _select_freshest_price_result(market_id: str, platform: str, sources=None):
    freshest = None
    freshest_ts = None
    candidate_sources = []
    for source, source_name in (sources or get_all_sources()):
        try:
            latest = _load_latest_price_from_source(source, source_name, market_id, platform)
            error = None
        except Exception as exc:
            latest = None
            error = str(exc)

        latest_ts = int(getattr(latest, "timestamp", 0) or 0) if latest is not None else None
        candidate, provenance = _build_candidate_source(
            source,
            source_name,
            market_id,
            platform,
            latest_ts=latest_ts,
            had_data=latest is not None,
            start_ts=latest_ts,
            end_ts=latest_ts,
            error=error,
        )
        candidate_sources.append(candidate)

        if latest is not None and (freshest is None or latest_ts > freshest_ts):
            freshest = (source_name, latest, provenance)
            freshest_ts = latest_ts

    if freshest is None:
        return None

    selected_source, latest, provenance = freshest
    for candidate in candidate_sources:
        if candidate["source_name"] == selected_source:
            candidate["selected"] = True

    return {
        "source_name": selected_source,
        "price": latest,
        "provenance": _selection_provenance_payload(
            selected_source=selected_source,
            provenance=provenance,
            candidate_sources=candidate_sources,
            selection_reason="freshest_latest_timestamp",
            selected_point_timestamp=int(getattr(latest, "timestamp", 0) or 0),
        ),
    }


def _select_freshest_price(market_id: str, platform: str):
    freshest = _select_freshest_price_result(market_id, platform)
    if freshest is None:
        return None
    return freshest["source_name"], freshest["price"]


def _load_history_from_source(source, source_name: str, market_id: str, platform: str, start_ts: int, end_ts: int):
    if source_name == "sqlite-cache":
        if not source.get_market(market_id):
            return []
        return _cache_get_price_history(source, market_id, start_ts, end_ts, platform=platform)
    return source.get_price_history(market_id, platform, start_ts, end_ts)


def _select_freshest_history_result(market_id: str, platform: str, start_ts: int, end_ts: int, sources=None):
    freshest = None
    freshest_ts = None
    candidate_sources = []
    for source, source_name in (sources or get_all_sources()):
        try:
            history = _load_history_from_source(source, source_name, market_id, platform, start_ts, end_ts)
            error = None
        except Exception as exc:
            history = []
            error = str(exc)

        latest_ts = int(getattr(history[-1], "timestamp", 0) or 0) if history else None
        candidate, provenance = _build_candidate_source(
            source,
            source_name,
            market_id,
            platform,
            latest_ts=latest_ts,
            had_data=bool(history),
            start_ts=start_ts,
            end_ts=end_ts,
            error=error,
        )
        candidate_sources.append(candidate)

        if history and (freshest is None or latest_ts > freshest_ts):
            freshest = (source_name, history, provenance)
            freshest_ts = latest_ts

    if freshest is None:
        return None

    selected_source, history, provenance = freshest
    for candidate in candidate_sources:
        if candidate["source_name"] == selected_source:
            candidate["selected"] = True

    return {
        "source_name": selected_source,
        "history": history,
        "provenance": _selection_provenance_payload(
            selected_source=selected_source,
            provenance=provenance,
            candidate_sources=candidate_sources,
            selection_reason="freshest_latest_timestamp",
            window_start_ts=start_ts,
            window_end_ts=end_ts,
            selected_point_timestamp=int(getattr(history[-1], "timestamp", 0) or 0),
        ),
    }


def _select_freshest_history(market_id: str, platform: str, start_ts: int, end_ts: int, sources=None):
    freshest = _select_freshest_history_result(market_id, platform, start_ts, end_ts, sources=sources)
    if freshest is None:
        return None
    return freshest["source_name"], freshest["history"]


def _fetch_pmxt_candles(client, market, start_ts: int, end_ts: int, interval_minutes: int) -> dict:
    condition_id = _candlestick_market_id(market)
    outcome_side = None
    if hasattr(client, "get_outcome_side"):
        try:
            outcome_side = client.get_outcome_side(condition_id, market.platform)
        except Exception:
            outcome_side = None
    if hasattr(client, "get_candlesticks_with_status"):
        result = client.get_candlesticks_with_status(condition_id, market.platform, start_ts, end_ts, interval_minutes)
        return {
            "points": list(result.get("points", [])),
            "status": str(result.get("status", "empty")),
            "error": result.get("error"),
            "outcome_side": outcome_side,
        }

    points = client.get_candlesticks(condition_id, market.platform, start_ts, end_ts, interval_minutes)
    return {
        "points": list(points),
        "status": "ok" if points else "empty",
        "error": None,
        "outcome_side": outcome_side,
    }


def _fetch_pmxt_orderbooks(client, market, start_ts: int, end_ts: int, limit: int = 100) -> dict:
    if hasattr(client, "get_orderbook_snapshots_with_status"):
        result = client.get_orderbook_snapshots_with_status(market.id, market.platform, start_ts, end_ts, limit)
        return {
            "snapshots": list(result.get("snapshots", [])),
            "status": str(result.get("status", "empty")),
            "error": result.get("error"),
        }

    snapshots = client.get_orderbook_snapshots(market.id, market.platform, start_ts, end_ts, limit)
    status = "ok" if any(getattr(snapshot, "bids", None) or getattr(snapshot, "asks", None) for snapshot in snapshots) else "empty"
    return {
        "snapshots": list(snapshots),
        "status": status,
        "error": None,
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
                    "active_only": {
                        "type": "boolean",
                        "default": True,
                        "description": "If true (default), prioritize unresolved markets for discovery. Set false to include resolved historical markets.",
                    },
                    "include_capabilities": {"type": "boolean", "default": False, "description": "Include backtest/history/sync capability annotations per market."},
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
                    "active_only": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "If true (default), filter out resolved/expired markets. "
                            "Set to false to include historical markets."
                        ),
                    },
                    "min_history_points": {
                        "type": "integer",
                        "default": 0,
                        "description": (
                            "Minimum number of price history points a market must have "
                            "to be included in results. Default: 0 (no filter)."
                        ),
                    },
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
        types.Tool(name="start_paper_trade", description="Start paper trading daemon", inputSchema={"type": "object", "properties": {"strategy_path": {"type": "string"}, "initial_cash": {"type": "number", "default": 10000}, "wait_for_ready": {"type": "boolean", "default": False, "description": "If true, poll until daemon is running (up to 30s) and return initial positions."}}, "required": ["strategy_path"]}),
        types.Tool(name="get_portfolio", description="Get paper portfolio status", inputSchema={"type": "object", "properties": {"portfolio_id": {"type": "string"}}, "required": ["portfolio_id"]}),
        types.Tool(name="flatten_portfolio", description="Flatten all open paper positions for a portfolio without stopping the daemon.", inputSchema={"type": "object", "properties": {"portfolio_id": {"type": "string"}, "best_effort": {"type": "boolean", "default": False, "description": "If true, continue flattening other positions after an individual close fails."}}, "required": ["portfolio_id"]}),
        types.Tool(name="stop_paper_trade", description="Stop paper trading daemon", inputSchema={"type": "object", "properties": {"portfolio_id": {"type": "string"}}, "required": ["portfolio_id"]}),
        types.Tool(name="list_paper_portfolios", description="List paper portfolios with optional filtering. This is the canonical portfolio listing tool.", inputSchema={"type": "object", "properties": {"status": {"type": "string", "enum": ["starting", "running", "stopped", "failed", "dead"]}, "active_only": {"type": "boolean", "default": False}, "limit": {"type": "integer", "default": 100}}}),
        types.Tool(name="list_paper_trades", description="Deprecated alias for list_paper_portfolios. Returns portfolio rows, not trade rows.", inputSchema={"type": "object", "properties": {"status": {"type": "string", "enum": ["starting", "running", "stopped", "failed", "dead"]}, "active_only": {"type": "boolean", "default": False}, "limit": {"type": "integer", "default": 100}}}),
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
        if name in PMXT_GUARDED_TOOLS:
            conflict = _pmxt_sidecar_conflict_payload()
            if conflict is not None:
                return _respond(conflict)

        if name == "get_markets":
            source, source_name = get_best_data_source()
            platform = args.get("platform", "all")
            market_ids = args.get("market_ids")
            category = args.get("category")
            tags = args.get("tags")
            limit = _bounded_int(args, "limit", 20, 1, 1000)
            active_only = bool(args.get("active_only", True))
            if market_ids and hasattr(source, "get_markets_by_ids"):
                markets = source.get_markets_by_ids(market_ids, platform=platform)
            elif market_ids and source_name == "sqlite-cache":
                markets = []
                for mid in market_ids:
                    m = cache.get_market(mid)
                    if m and (platform == "all" or m.platform.value == platform):
                        markets.append(m)
            else:
                markets = _get_research_markets(
                    source,
                    source_name,
                    platform=platform,
                    category=category,
                    tags=tags,
                    limit=limit,
                    market_ids=None,
                    active_only=active_only,
                )
                if category:
                    markets = [m for m in markets if _market_matches_category(m, category)]
                markets = markets[:limit]
            include_caps = bool(args.get("include_capabilities", False))
            if include_caps:
                caps = _compute_capabilities(markets, cache)
                market_dicts = [
                    m.__dict__ | {"platform": m.platform.value, "market_type": m.market_type.value, "capabilities": caps.get(m.id, {})}
                    for m in markets
                ]
            else:
                market_dicts = [
                    m.__dict__ | {"platform": m.platform.value, "market_type": m.market_type.value}
                    for m in markets
                ]
            return _respond({
                "ok": True,
                "data_source": source_name,
                "count": len(markets),
                "markets": market_dicts,
            })

        if name == "get_price":
            market_id = args["market_id"]
            platform = args.get("platform", "polymarket")
            freshest = _select_freshest_price_result(market_id, platform)
            if freshest is not None:
                return _respond(
                    {
                        "ok": True,
                        "data_source": freshest["source_name"],
                        "market_id": market_id,
                        "price": freshest["price"].__dict__,
                        "provenance": freshest["provenance"],
                        "timestamp_format": "unix_seconds",
                    }
                )
            return _respond(
                _error_payload(
                    "MarketNotFound",
                    f"No price data found for {market_id}",
                    fix=f"Call sync_data(market_ids=['{market_id}']) or check market_id is correct.",
                )
            )

        if name == "get_history":
            market_id = args["market_id"]
            days = _bounded_int(args, "days", 7, 1, 3650)
            end_ts = int(time.time())
            start_ts = end_ts - days * 24 * 3600
            platform = args.get("platform", "polymarket")

            freshest = _select_freshest_history_result(market_id, platform, start_ts, end_ts)
            if freshest is None:
                # Distinguish "market exists but no data in window" from "market unknown"
                known_price = _select_freshest_price_result(market_id, platform)
                if known_price is not None:
                    # Market exists — just no data in the requested lookback
                    latest = known_price["price"]
                    payload = {
                        "ok": True,
                        "data_source": known_price["source_name"],
                        "market_id": market_id,
                        "days": days,
                        "analytics": _compute_history_analytics([], end_ts),
                        "provenance": {
                            **known_price["provenance"],
                            "selection_reason": "known_market_no_points_in_window",
                            "window_start_ts": start_ts,
                            "window_end_ts": end_ts,
                        },
                        "timestamp_format": "unix_seconds",
                        "warning": (
                            f"No history points found in the requested {days}-day lookback window. "
                            f"The market has data outside this range (last point: "
                            f"{datetime.fromtimestamp(int(latest.timestamp), tz=UTC).isoformat()})."
                        ),
                    }
                    return _respond(payload)
                return _respond(
                    _error_payload(
                        "MarketNotFound",
                        f"No history data found for {market_id}",
                        fix=f"Call sync_data(market_ids=['{market_id}']) and retry.",
                    )
                )
            history = freshest["history"]

            include_raw = bool(args.get("include_raw", False))
            payload = {
                "ok": True,
                "data_source": freshest["source_name"],
                "market_id": market_id,
                "days": days,
                "analytics": _compute_history_analytics(history, end_ts),
                "provenance": freshest["provenance"],
                "timestamp_format": "unix_seconds",
            }
            if include_raw:
                payload["history"] = [h.__dict__ for h in history]
                payload["history_timestamp_format"] = "unix_seconds"
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
                        initial_cash=_bounded_float(args, "initial_cash", 10000.0, 1.0, 1e9),
                        status="running",
                        created_at=now_ts,
                    )
                )
                session.commit()

            progress_callback = _make_mcp_backtest_progress_callback(run_id)

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
                        initial_cash=_bounded_float(args, "initial_cash", 10000.0, 1.0, 1e9),
                        schedule_interval_minutes=load_config()["schedule_interval_minutes"],
                        max_markets=int(args["max_markets"]) if args.get("max_markets") is not None else None,
                        fidelity=str(args.get("fidelity", "exact_trade")),
                        execution_mode=execution_mode,
                    ),
                    progress_callback=progress_callback,
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
                if isinstance(exc, (AgentTraderError, RuntimeError)):
                    return _respond(_error_payload("StrategyError", str(exc), fix="Fix the strategy and retry."))
                return _respond(_error_payload("InternalError", str(exc)))

        if name == "research_markets":
            days = _bounded_int(args, "days", 7, 1, 3650)
            platform = args.get("platform", "all")
            category = args.get("category")
            tags = args.get("tags")
            market_ids = args.get("market_ids")
            limit = _bounded_int(args, "limit", 20, 1, 1000)
            sync_limit = _bounded_int(args, "sync_limit", 100, 1, 1000)
            min_history_points = _bounded_int(args, "min_history_points", 0, 0, 100000)
            include_raw = bool(args.get("include_raw", False))
            active_only = bool(args.get("active_only", True))
            candidate_limit = limit
            if not market_ids:
                if min_history_points > 0:
                    candidate_limit = min(max(limit * 10, limit), 250)
                elif category and active_only:
                    candidate_limit = min(max(limit * 5, limit), 100)

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
                markets = []

            markets = _get_research_markets(
                source,
                source_name,
                platform=platform,
                category=category,
                tags=tags,
                limit=candidate_limit,
                market_ids=market_ids,
                active_only=active_only,
            )

            # Filter out resolved markets unless active_only=false
            if active_only:
                markets = [m for m in markets if not _is_market_resolved(m)]
            if category:
                markets = [m for m in markets if _market_matches_category(m, category)]

            # Get history for each market — try all sources per market
            end_ts = int(time.time())
            start_ts = end_ts - days * 24 * 3600
            all_sources = get_all_sources()
            history = []
            history_errors = []
            selected_markets = []
            target_count = len(markets) if market_ids else limit
            for market in markets:
                try:
                    mid = market.id if hasattr(market, "id") else market["id"]
                    plat = market.platform.value if hasattr(market.platform, "value") else str(market.platform)
                    freshest = _select_freshest_history(mid, plat, start_ts, end_ts, sources=all_sources)
                    selected_history_source = None
                    pts = []
                    if freshest is not None:
                        selected_history_source, pts = freshest
                    analytics = _compute_history_analytics(pts, end_ts)
                    if analytics["points"] < min_history_points:
                        continue
                    entry = {
                        "ok": True,
                        "market_id": mid,
                        "days": days,
                        "analytics": analytics,
                        "timestamp_format": "unix_seconds",
                    }
                    if selected_history_source is not None:
                        entry["data_source"] = selected_history_source
                        source_obj = next(
                            (candidate_source for candidate_source, candidate_name in all_sources if candidate_name == selected_history_source),
                            None,
                        )
                        if source_obj is not None:
                            candidate_sources = []
                            for candidate_source, candidate_name in all_sources:
                                candidate_entry, _ = _build_candidate_source(
                                    candidate_source,
                                    candidate_name,
                                    mid,
                                    plat,
                                    latest_ts=int(getattr(pts[-1], "timestamp", 0) or 0) if pts and candidate_name == selected_history_source else None,
                                    had_data=candidate_name == selected_history_source and bool(pts),
                                    start_ts=start_ts,
                                    end_ts=end_ts,
                                )
                                candidate_entry["selected"] = candidate_name == selected_history_source
                                candidate_sources.append(candidate_entry)
                            entry["provenance"] = _selection_provenance_payload(
                                selected_source=selected_history_source,
                                provenance=_load_source_provenance(source_obj, selected_history_source, mid, plat, start_ts=start_ts, end_ts=end_ts),
                                candidate_sources=candidate_sources,
                                selection_reason="freshest_latest_timestamp",
                                window_start_ts=start_ts,
                                window_end_ts=end_ts,
                                selected_point_timestamp=int(getattr(pts[-1], "timestamp", 0) or 0) if pts else None,
                            )
                    if analytics["points"] == 0:
                        entry["warning"] = "No price data found in the requested lookback window."
                    if include_raw:
                        entry["history"] = [h.__dict__ for h in pts]
                        entry["history_timestamp_format"] = "unix_seconds"
                    selected_markets.append(market)
                    history.append(entry)
                    if len(selected_markets) >= target_count:
                        break
                except Exception as exc:
                    history_errors.append({"market_id": getattr(market, "id", "?"), "error": str(exc)})
            markets = selected_markets

            # Build analytics lookup for inline analytics on market objects
            analytics_by_id = {h["market_id"]: h.get("analytics", {}) for h in history}

            caps = _compute_capabilities(markets, cache)
            candidate_sources = [
                _build_candidate_source(candidate_source, candidate_name, "*", platform, latest_ts=None, had_data=True)[0]
                for candidate_source, candidate_name in all_sources
            ]
            for candidate in candidate_sources:
                if candidate["source_name"] == source_name:
                    candidate["selected"] = True
            payload = {
                "ok": len(history_errors) == 0,
                "data_source": source_name,
                "provenance": _selection_provenance_payload(
                    selected_source=source_name,
                    provenance=_load_source_provenance(source, source_name, "*", platform),
                    candidate_sources=candidate_sources,
                    selection_reason="best_available_source_priority",
                    window_start_ts=start_ts,
                    window_end_ts=end_ts,
                ),
                "markets": [
                    m.__dict__ | {"platform": m.platform.value, "market_type": m.market_type.value, "capabilities": caps.get(m.id, {}), "analytics": analytics_by_id.get(m.id, {})}
                    for m in markets
                ],
                "history": history,
                "history_errors": history_errors,
                "count": len(markets),
                "timestamp_format": "unix_seconds",
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
            rows = cache.list_backtest_runs(limit=100)
            runs = []
            for row in rows:
                progress = _extract_backtest_progress(row.results_json)
                runs.append(
                    {
                        "id": row.id,
                        "status": row.status,
                        "strategy_path": row.strategy_path,
                        "progress_pct": (progress or {}).get("percent_complete"),
                        "processed_units": (progress or {}).get("processed_units"),
                        "work_unit_label": (progress or {}).get("work_unit_label"),
                    }
                )
            return _respond({"ok": True, "runs": runs})

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
            initial_cash = _bounded_float(args, "initial_cash", 10000.0, 1.0, 1e9)
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
            time.sleep(1.0)
            runtime_status = read_runtime_status(portfolio_id)
            if runtime_status and runtime_status.get("state") == "failed":
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
                        "Paper trading daemon failed during live startup",
                        fix="Check runtime_status.last_error and daemon log for details.",
                        runtime_status=runtime_status,
                    )
                )
            if runtime_status is None:
                deadline = time.time() + 4.0
                while time.time() < deadline and proc.poll() is None:
                    runtime_status = read_runtime_status(portfolio_id)
                    if runtime_status is not None:
                        break
                    time.sleep(0.25)
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

            response = {"ok": True, "portfolio_id": portfolio_id, "pid": pid, "live_status": runtime_status}

            wait_for_ready = bool(args.get("wait_for_ready", False))
            if wait_for_ready:
                deadline = time.time() + 30.0
                while time.time() < deadline:
                    rs = read_runtime_status(portfolio_id)
                    if rs and rs.get("state") == "running":
                        response["live_status"] = rs
                        break
                    if rs and rs.get("state") == "failed":
                        response["live_status"] = rs
                        response["warning"] = "Daemon entered failed state during wait."
                        break
                    if not _pid_alive(pid):
                        response["warning"] = "Daemon process died during wait."
                        break
                    time.sleep(0.5)
                else:
                    response["warning"] = "Timed out waiting for daemon to reach running state (30s)."
                # Include initial positions if daemon is running
                rs = response.get("live_status") or {}
                if rs.get("state") == "running":
                    with get_session(get_engine()) as session:
                        row = session.get(PaperPortfolio, portfolio_id)
                        if row:
                            response["cash_balance"] = row.cash_balance
                    positions = cache.get_open_positions(portfolio_id)
                    response["positions"] = [
                        {
                            "market_id": pos.market_id,
                            "platform": pos.platform,
                            "side": pos.side,
                            "contracts": pos.contracts,
                            "avg_cost": pos.avg_cost,
                        }
                        for pos in positions
                    ]

            return _respond(response)

        if name == "get_portfolio":
            portfolio_id = args["portfolio_id"]
            p = cache.get_portfolio(portfolio_id)
            if not p:
                return _respond(
                    _error_payload(
                        "NotFound",
                        "portfolio not found",
                        fix=f"Call list_paper_portfolios then retry with a valid portfolio_id (missing: {portfolio_id}).",
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
            elif str(p.status or "").lower() == "stopped" and p.pid is not None:
                with get_session(get_engine()) as session:
                    row = session.get(PaperPortfolio, p.id)
                    if row:
                        row.pid = None
                        session.commit()
                p = cache.get_portfolio(portfolio_id)
            runtime_status = read_runtime_status(p.id)
            runtime_status = _normalize_runtime_status_for_portfolio(p, runtime_status)
            live_price_map = _build_portfolio_live_price_map(p, runtime_status)
            positions = cache.get_open_positions(p.id)
            out = []
            unrealized = 0.0
            for pos in positions:
                latest = _cache_get_latest_price(cache, pos.market_id, platform=pos.platform)
                live_yes_price = live_price_map.get(pos.market_id)
                if live_yes_price is not None:
                    current = _select_position_mark_price(pos.side, live_yes_price)
                elif latest is not None:
                    current = _select_position_mark_price(
                        pos.side,
                        getattr(latest, "yes_price", None),
                        getattr(latest, "no_price", None),
                    )
                else:
                    current = float(pos.avg_cost)
                current = float(current if current is not None else pos.avg_cost)
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
                    "pid": _normalized_portfolio_pid(p),
                    "initial_cash": p.initial_cash,
                    "cash_balance": p.cash_balance,
                    "portfolio_value": p.cash_balance + sum(i["contracts"] * i["current_price"] for i in out),
                    "unrealized_pnl": unrealized,
                    "positions": out,
                    "last_reload": p.last_reload,
                    "reload_count": p.reload_count or 0,
                    "last_live_update": runtime_status.get("last_live_update") if runtime_status else None,
                    "last_live_update_iso": _iso_or_none(runtime_status.get("last_live_update")) if runtime_status else None,
                    "markets_live": runtime_status.get("markets_with_live_price") if runtime_status else None,
                    "markets_degraded": runtime_status.get("markets_degraded") if runtime_status else None,
                    "live_status": runtime_status,
                    "timestamp_format": "unix_seconds",
                }
            )

        if name == "flatten_portfolio":
            portfolio_id = args["portfolio_id"]
            p = cache.get_portfolio(portfolio_id)
            if not p:
                return _respond(
                    _error_payload(
                        "NotFound",
                        "portfolio not found",
                        fix=f"Call list_paper_portfolios then retry with a valid portfolio_id (missing: {portfolio_id}).",
                    )
                )
            best_effort = bool(args.get("best_effort", False))
            flatten_result = _flatten_portfolio_positions(p, cache, best_effort=best_effort)
            payload = {
                "portfolio_id": p.id,
                "best_effort": best_effort,
                "results": flatten_result["results"],
                "positions_attempted": flatten_result["positions_attempted"],
                "positions_closed": flatten_result["positions_closed"],
                "positions_failed": flatten_result["positions_failed"],
                "remaining_positions": flatten_result["remaining_positions"],
                "realized_pnl": flatten_result["realized_pnl"],
                "cash_balance": flatten_result["cash_balance"],
                "portfolio_value_after_flatten": flatten_result["portfolio_value_after_flatten"],
                "timestamp_format": "unix_seconds",
            }
            if flatten_result["ok"]:
                payload["ok"] = True
                return _respond(payload)
            return _respond(
                _error_payload(
                    "PartialFlattenFailure" if best_effort else "FlattenFailed",
                    "One or more positions could not be flattened.",
                    fix="Inspect the per-position errors, refresh prices, and retry flatten_portfolio.",
                    **payload,
                )
            )

        if name == "stop_paper_trade":
            portfolio_id = args["portfolio_id"]
            p = cache.get_portfolio(portfolio_id)
            if not p:
                return _respond(
                    _error_payload(
                        "NotFound",
                        "portfolio not found",
                        fix=f"Call list_paper_portfolios then retry with a valid portfolio_id (missing: {portfolio_id}).",
                    )
                )
            if p.pid:
                try:
                    if sys.platform == "win32":
                        import ctypes
                        kernel32 = ctypes.windll.kernel32
                        handle = kernel32.OpenProcess(0x0001, False, int(p.pid))
                        if handle:
                            kernel32.TerminateProcess(handle, 1)
                            kernel32.CloseHandle(handle)
                        else:
                            os.kill(int(p.pid), signal.SIGTERM)
                    else:
                        os.kill(int(p.pid), signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass  # Process already dead
            stopped_at = int(datetime.now(tz=UTC).timestamp())
            with get_session(get_engine()) as session:
                row = session.get(PaperPortfolio, p.id)
                if row:
                    row.status = "stopped"
                    row.stopped_at = stopped_at
                    row.pid = None
                    session.commit()
            try:
                status_path = runtime_status_path(p.id)
                if status_path.exists():
                    status_path.unlink()
            except OSError:
                pass
            return _respond({"ok": True, "portfolio_id": p.id, "stopped": True, "pid": None, "stopped_at": stopped_at, "stopped_at_iso": _iso_or_none(stopped_at), "timestamp_format": "unix_seconds"})

        if name in {"list_paper_trades", "list_paper_portfolios"}:
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
                elif str(p.status or "").lower() == "stopped" and p.pid is not None:
                    with get_session(get_engine()) as session:
                        row = session.get(PaperPortfolio, p.id)
                        if row:
                            row.pid = None
                            session.commit()
                    p.pid = None

            wanted_status = args.get("status")
            active_only = bool(args.get("active_only", False))
            if wanted_status:
                rows = [p for p in rows if str(p.status or "").lower() == str(wanted_status).lower()]
            if active_only:
                rows = [p for p in rows if _status_is_active(p.status)]

            status_priority = {"running": 0, "starting": 1, "failed": 2, "dead": 3, "stopped": 4}
            rows.sort(key=lambda p: (status_priority.get(str(p.status or "").lower(), 9), -int(getattr(p, "started_at", 0) or 0)))
            rows = rows[:_bounded_int(args, "limit", 100, 1, 1000)]
            portfolios = []
            for p in rows:
                rs = _normalize_runtime_status_for_portfolio(p, read_runtime_status(p.id)) or {}
                portfolios.append({
                    "id": p.id,
                    "status": p.status,
                    "pid": _normalized_portfolio_pid(p),
                    "started_at": getattr(p, "started_at", None),
                    "started_at_iso": _iso_or_none(getattr(p, "started_at", None)),
                    "stopped_at": getattr(p, "stopped_at", None),
                    "stopped_at_iso": _iso_or_none(getattr(p, "stopped_at", None)),
                    "last_live_update": rs.get("last_live_update"),
                    "last_live_update_iso": _iso_or_none(rs.get("last_live_update")),
                    "markets_live": rs.get("markets_with_live_price"),
                    "markets_degraded": rs.get("markets_degraded"),
                })
            active_portfolios = [portfolio for portfolio in portfolios if _status_is_active(portfolio.get("status"))]
            payload = {
                "ok": True,
                "portfolios": portfolios,
                "active_portfolios": active_portfolios,
                "active_count": len(active_portfolios),
                "inactive_count": max(len(portfolios) - len(active_portfolios), 0),
                "timestamp_format": "unix_seconds",
            }
            if name == "list_paper_trades":
                payload["deprecated"] = True
                payload["canonical_tool"] = "list_paper_portfolios"
            return _respond(payload)

        if name == "sync_data":
            client = PmxtClient()
            days = _bounded_int(args, "days", 7, 1, 3650)
            platform = args.get("platform", "all")
            market_ids = args.get("market_ids")
            limit = _bounded_int(args, "limit", 100, 1, 1000)
            category = args.get("category")
            resolved = args.get("resolved", False)
            granularity = args.get("granularity", "hourly")
            granularity_map = {"minute": 1, "hourly": 60, "daily": 1440}
            interval_minutes = granularity_map.get(granularity, 60)
            markets = []
            resolved_market_ids = set()
            if market_ids:
                markets, resolved_market_ids = _resolve_market_ids_for_sync(
                    market_ids,
                    platform=platform,
                    category=category,
                    include_resolved=bool(resolved),
                )

            remaining_market_ids = None
            if market_ids:
                remaining_market_ids = [
                    market_id
                    for market_id in market_ids
                    if str(market_id).strip().lower() not in resolved_market_ids
                ]

            if not market_ids or remaining_market_ids:
                mk = {"platform": platform, "limit": limit}
                if remaining_market_ids:
                    mk["market_ids"] = remaining_market_ids
                if category:
                    mk["category"] = category
                if resolved:
                    mk["resolved"] = resolved

                live_markets = client.get_markets(**mk)
                seen_market_keys = {
                    (_market_platform_value(market), str(getattr(market, "id", "")))
                    for market in markets
                }
                for market in live_markets:
                    market_key = (_market_platform_value(market), str(getattr(market, "id", "")))
                    if market_key in seen_market_keys:
                        continue
                    seen_market_keys.add(market_key)
                    markets.append(market)

            start_ts = int(time.time()) - days * 24 * 3600
            end_ts = int(time.time())
            replace_start_ts, replace_end_ts = _pmxt_window_bounds(start_ts, end_ts, interval_minutes)
            ob_store = OrderBookStore()
            pp = 0
            ob_files = 0
            synced = 0
            markets_with_price_points = 0
            markets_with_orderbooks = 0
            markets_with_live_data = 0
            errors = []
            warnings = []
            market_results = []
            for m in markets:
                try:
                    cache.upsert_market(m)
                    candles_result = _fetch_pmxt_candles(client, m, start_ts, end_ts, interval_minutes)
                    raw_candles = list(candles_result["points"])
                    candles = raw_candles
                    gran_label = {"minute": "1m", "hourly": "1h", "daily": "1d"}.get(granularity, "1h")
                    market_platform = _market_platform_value(m)
                    outcome_side = str(candles_result.get("outcome_side") or "").strip().lower() or None
                    orientation_warning = None
                    batch_repaired = False
                    batch_inverted = False
                    repair_count = 0
                    if raw_candles:
                        live_snapshot = client.get_live_snapshot(m.id, m.platform) if hasattr(client, "get_live_snapshot") else None
                        reference_price = None
                        live_point = live_snapshot.get("price") if isinstance(live_snapshot, dict) else None
                        if live_point is not None:
                            reference_price = float(getattr(live_point, "yes_price", 0.0))
                            if outcome_side == "no":
                                reference_price = max(0.0, min(1.0, 1.0 - reference_price))
                        else:
                            latest_cached = _cache_get_latest_price(cache, m.id, platform=market_platform)
                            if latest_cached is not None:
                                reference_price = float(latest_cached.yes_price)
                        normalized_candles = _normalize_pmxt_candles_to_yes_space(
                            raw_candles,
                            outcome_side=outcome_side,
                            reference_yes_price=reference_price,
                        )
                        if normalized_candles["ok"]:
                            candles = list(normalized_candles["points"])
                            batch_inverted = bool(normalized_candles["batch_inverted"])
                            repair_count = len({issue["timestamp"] for issue in normalized_candles["repairs"]})
                            batch_repaired = repair_count > 0
                            _replace_pmxt_history_window(
                                cache,
                                m.id,
                                market_platform,
                                replace_start_ts,
                                replace_end_ts,
                                candles,
                                granularity=gran_label,
                            )
                            pp += len(candles)
                            if candles:
                                markets_with_price_points += 1
                            if batch_inverted:
                                warnings.append({
                                    "market_id": m.id,
                                    "type": "PriceOrientationNormalized",
                                    "message": f"Normalized PMXT candles for {m.id} into YES-price space before writing.",
                                    "detail": {
                                        "outcome_side": outcome_side,
                                        "inversion_reason": normalized_candles["inversion_reason"],
                                        "batch_mismatch": normalized_candles["batch_mismatch"],
                                    },
                                })
                            if batch_repaired:
                                warnings.append({
                                    "market_id": m.id,
                                    "type": "LocalizedPriceRepair",
                                    "message": f"Repaired {repair_count} localized inverted PMXT candle(s) for {m.id} before writing.",
                                    "detail": {
                                        "repair_count": repair_count,
                                        "repairs": normalized_candles["repairs"],
                                    },
                                })
                        else:
                            candles = []
                            orientation_warning = {
                                "outcome_side": outcome_side,
                                "inversion_reason": normalized_candles["inversion_reason"],
                                "batch_mismatch": normalized_candles["batch_mismatch"],
                                "repairs": normalized_candles["repairs"],
                                "residual_issues": normalized_candles["residual_issues"],
                            }
                    if orientation_warning is not None:
                        warnings.append({
                            "market_id": m.id,
                            "type": "PriceOrientationMismatch",
                            "message": (
                                f"Rejected PMXT candles for {m.id} because the series could not be "
                                f"normalized into a coherent YES-price history."
                            ),
                            "detail": orientation_warning,
                        })

                    orderbook_result = _fetch_pmxt_orderbooks(client, m, start_ts, end_ts, 100)
                    ob = orderbook_result["snapshots"]
                    written_files = ob_store.write(market_platform, m.id, ob)
                    ob_files += written_files
                    if written_files > 0:
                        markets_with_orderbooks += 1

                    has_live_data = bool(candles) or written_files > 0
                    if has_live_data:
                        markets_with_live_data += 1

                    market_warning_types = []
                    if candles_result["status"] == "error":
                        warnings.append({
                            "market_id": m.id,
                            "type": "CandlesFetchError",
                            "message": f"PMXT candles fetch failed for {m.id}",
                            "detail": candles_result["error"],
                        })
                        market_warning_types.append("CandlesFetchError")
                    elif orientation_warning is not None:
                        market_warning_types.append("PriceOrientationMismatch")
                    else:
                        if batch_inverted:
                            market_warning_types.append("PriceOrientationNormalized")
                        if batch_repaired:
                            market_warning_types.append("LocalizedPriceRepair")
                    if (
                        candles_result["status"] != "error"
                        and not candles
                        and orientation_warning is None
                        and not batch_inverted
                        and not batch_repaired
                    ):
                        market_warning_types.append("NoPricePoints")

                    if orderbook_result["status"] == "error":
                        warnings.append({
                            "market_id": m.id,
                            "type": "OrderbookFetchError",
                            "message": f"PMXT orderbook fetch failed for {m.id}",
                            "detail": orderbook_result["error"],
                        })
                        market_warning_types.append("OrderbookFetchError")
                    elif written_files == 0:
                        market_warning_types.append("NoOrderbooks")

                    if not has_live_data:
                        warnings.append({
                            "market_id": m.id,
                            "type": "NoLiveData",
                            "message": f"No live candles or orderbook snapshots were returned for {m.id} in the requested window.",
                        })
                        market_warning_types.append("NoLiveData")

                    cache.mark_market_synced(m.id, int(time.time()))
                    synced += 1
                    market_results.append({
                        "market_id": m.id,
                        "platform": market_platform,
                        "price_points_fetched": len(candles),
                        "orderbook_files_written": written_files,
                        "has_live_data": has_live_data,
                        "candles_status": candles_result["status"],
                        "orderbook_status": orderbook_result["status"],
                        "price_orientation_mismatch": orientation_warning is not None,
                        "price_orientation_repaired": batch_inverted or batch_repaired,
                        "price_orientation_repair_count": repair_count,
                        "warning_types": market_warning_types,
                    })
                except Exception as exc:
                    errors.append({"market_id": m.id, "error": str(exc)})

            if market_ids and synced == 0 and len(errors) == 0:
                return _respond({
                    "ok": False,
                    "error": "NoMarketsFound",
                    "message": f"None of the {len(market_ids)} requested market_ids were found.",
                    "fix": (
                        "These market_ids may be historical/expired and unavailable via the live API. "
                        "Use get_markets or get_history to query parquet data for historical markets. "
                        "If these are active markets, check that PMXT credentials are configured."
                    ),
                })

            result = {
                "ok": len(errors) == 0,
                "markets_synced": synced,
                "markets_processed": synced,
                "price_points_fetched": pp,
                "orderbook_files_written": ob_files,
                "markets_with_price_points": markets_with_price_points,
                "markets_with_orderbooks": markets_with_orderbooks,
                "markets_with_live_data": markets_with_live_data,
                "market_results": market_results,
                "errors": errors,
            }
            if warnings:
                result["warnings"] = warnings
            if synced > 0 and markets_with_live_data == 0 and not errors:
                result["warning"] = (
                    "Markets were processed, but no live candles or orderbook snapshots were returned "
                    "for the requested sync window."
                )
            if synced == 0 and not errors and not market_ids and (category or platform != "all"):
                result["warning"] = (
                    "No markets matched your filters. "
                    "Try broadening category/platform or check available markets with get_markets."
                )
            # Invalidate cached sources so subsequent calls see freshly synced data
            invalidate_source_cache()
            return _respond(result)

        if name == "debug_data_sources":
            diag = {"ok": True, "sources": {}}

            # 1. DuckDB normalized index
            index_path = BACKTEST_INDEX_PATH
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
            data_dir = SHARED_DATA_DIR
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
            db_path = DB_PATH
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

    _ensure_pmxt_sidecar_safe()

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
