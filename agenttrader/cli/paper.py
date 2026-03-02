# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import hashlib
import os
import signal
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table
from sqlalchemy import and_, select

from agenttrader.cli.utils import emit_json, ensure_initialized, json_errors
from agenttrader.cli.validate import validate_strategy_file
from agenttrader.config import load_config
from agenttrader.core.paper_daemon import PaperDaemon, read_runtime_status
from agenttrader.data.cache import DataCache
from agenttrader.db import get_engine, get_session
from agenttrader.db.schema import PaperPortfolio, Position, Trade
from agenttrader.errors import AgentTraderError


def _terminate_pid(pid: int) -> None:
    """Terminate a process by PID, using the correct method per platform."""
    if sys.platform == "win32":
        # On Windows, SIGTERM is not deliverable cross-process via os.kill.
        # Use os.kill with SIGTERM which maps to TerminateProcess on Windows,
        # but wrap with kernel32 for reliable detached-process termination.
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_TERMINATE = 0x0001
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if handle:
            kernel32.TerminateProcess(handle, 1)
            kernel32.CloseHandle(handle)
        else:
            # Fallback: try os.kill anyway (works for some process types)
            os.kill(pid, signal.SIGTERM)
    else:
        os.kill(pid, signal.SIGTERM)


@click.group("paper")
def paper_group() -> None:
    """Manage paper trading daemons."""


@paper_group.command("start")
@click.argument("strategy_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--cash", "initial_cash", type=float, default=None)
@click.option("--no-daemon", is_flag=True)
@click.option("--json", "json_output", is_flag=True)
@json_errors
def paper_start(strategy_path: str, initial_cash: float | None, no_daemon: bool, json_output: bool) -> None:
    ensure_initialized()
    validation = validate_strategy_file(strategy_path)
    if not validation["valid"]:
        payload = {
            "ok": False,
            "error": "StrategyValidationError",
            "message": "Strategy validation failed",
            "errors": validation["errors"],
            "warnings": validation["warnings"],
        }
        if json_output:
            emit_json(payload)
            raise click.exceptions.Exit(1)
        raise AgentTraderError("StrategyValidationError", "Strategy validation failed", payload)

    cfg = load_config()
    initial_cash = float(initial_cash if initial_cash is not None else cfg["default_initial_cash"])

    strategy_file = Path(strategy_path)
    portfolio_id = str(uuid.uuid4())
    now_ts = int(datetime.now(tz=UTC).timestamp())
    strategy_hash = hashlib.sha256(strategy_file.read_bytes()).hexdigest()

    db_engine = get_engine()
    with get_session(db_engine) as session:
        session.add(
            PaperPortfolio(
                id=portfolio_id,
                strategy_path=str(strategy_file.resolve()),
                strategy_hash=strategy_hash,
                initial_cash=initial_cash,
                cash_balance=initial_cash,
                status="running",
                pid=None,
                started_at=now_ts,
                stopped_at=None,
                last_reload=None,
                reload_count=0,
            )
        )
        session.commit()

    daemon = PaperDaemon(portfolio_id=portfolio_id, strategy_path=str(strategy_file), initial_cash=initial_cash)

    if no_daemon:
        if json_output:
            emit_json({"ok": True, "portfolio_id": portfolio_id, "pid": os.getpid(), "mode": "blocking"})
        daemon._run()
        return

    proc = daemon.start_as_daemon()
    pid = proc.pid
    with get_session(db_engine) as session:
        row = session.get(PaperPortfolio, portfolio_id)
        if row:
            row.pid = pid
            session.commit()

    payload = {"ok": True, "portfolio_id": portfolio_id, "pid": pid}
    if json_output:
        emit_json(payload)
    else:
        click.echo(f"Started paper daemon {portfolio_id} pid={pid}")


@paper_group.command("stop")
@click.argument("portfolio_id", required=False)
@click.option("--all", "stop_all", is_flag=True)
@click.option("--json", "json_output", is_flag=True)
@json_errors
def paper_stop(portfolio_id: str | None, stop_all: bool, json_output: bool) -> None:
    ensure_initialized()
    if not stop_all and not portfolio_id:
        raise click.UsageError("Provide <portfolio_id> or --all")

    db_engine = get_engine()
    with get_session(db_engine) as session:
        if stop_all:
            rows = list(session.scalars(select(PaperPortfolio).where(PaperPortfolio.status == "running")).all())
        else:
            row = session.get(PaperPortfolio, portfolio_id)
            rows = [row] if row else []

        stopped = []
        for row in rows:
            if row is None:
                continue
            if row.pid:
                try:
                    _terminate_pid(int(row.pid))
                except (ProcessLookupError, OSError):
                    pass
            row.status = "stopped"
            row.stopped_at = int(datetime.now(tz=UTC).timestamp())
            stopped.append(row.id)
        session.commit()

    payload = {"ok": True, "stopped": stopped}
    if json_output:
        emit_json(payload)
    else:
        click.echo(f"Stopped {len(stopped)} portfolio(s)")


@paper_group.command("status")
@click.argument("portfolio_id")
@click.option("--json", "json_output", is_flag=True)
@json_errors
def paper_status(portfolio_id: str, json_output: bool) -> None:
    ensure_initialized()
    cache = DataCache(get_engine())
    portfolio = cache.get_portfolio(portfolio_id)
    if not portfolio:
        raise AgentTraderError("NotFound", f"Portfolio not found: {portfolio_id}")

    positions_rows = cache.get_open_positions(portfolio_id)
    runtime_status = read_runtime_status(portfolio_id)
    live_price_map = {
        item["market_id"]: item.get("current_price")
        for item in (runtime_status or {}).get("markets", [])
        if item.get("market_id")
    }
    positions = []
    unrealized_total = 0.0
    for pos in positions_rows:
        latest = cache.get_latest_price(pos.market_id)
        live_price = live_price_map.get(pos.market_id)
        current_price = live_price if live_price is not None else (latest.yes_price if latest else pos.avg_cost)
        upnl = (current_price - pos.avg_cost) * pos.contracts
        unrealized_total += upnl
        market = cache.get_market(pos.market_id)
        positions.append(
            {
                "market_id": pos.market_id,
                "market_title": market.title if market else pos.market_id,
                "platform": pos.platform,
                "side": pos.side,
                "contracts": pos.contracts,
                "avg_cost": pos.avg_cost,
                "current_price": current_price,
                "unrealized_pnl": upnl,
            }
        )

    payload = {
        "ok": True,
        "portfolio_id": portfolio.id,
        "strategy_path": portfolio.strategy_path,
        "status": portfolio.status,
        "pid": portfolio.pid,
        "started_at": portfolio.started_at,
        "initial_cash": portfolio.initial_cash,
        "cash_balance": portfolio.cash_balance,
        "portfolio_value": portfolio.cash_balance + sum(p["contracts"] * p["current_price"] for p in positions),
        "unrealized_pnl": unrealized_total,
        "positions": positions,
        "last_reload": portfolio.last_reload,
        "reload_count": portfolio.reload_count or 0,
    }
    payload["last_live_update"] = runtime_status.get("last_live_update") if runtime_status else None
    payload["markets_live"] = runtime_status.get("markets_with_live_price") if runtime_status else None
    payload["markets_degraded"] = runtime_status.get("markets_degraded") if runtime_status else None
    payload["live_status"] = runtime_status

    if json_output:
        emit_json(payload)
    else:
        click.echo(f"{portfolio.id} status={portfolio.status} value={payload['portfolio_value']:.2f}")


@paper_group.command("list")
@click.option("--json", "json_output", is_flag=True)
@json_errors
def paper_list(json_output: bool) -> None:
    ensure_initialized()
    cache = DataCache(get_engine())
    rows = cache.list_paper_portfolios()
    portfolios = []
    for p in rows:
        rs = read_runtime_status(p.id) or {}
        portfolios.append({
            "id": p.id,
            "strategy_path": p.strategy_path,
            "status": p.status,
            "pid": p.pid,
            "started_at": p.started_at,
            "stopped_at": p.stopped_at,
            "cash_balance": p.cash_balance,
            "initial_cash": p.initial_cash,
            "last_reload": p.last_reload,
            "reload_count": p.reload_count or 0,
            "last_live_update": rs.get("last_live_update"),
            "markets_live": rs.get("markets_with_live_price"),
            "markets_degraded": rs.get("markets_degraded"),
        })
    payload = {"ok": True, "portfolios": portfolios}
    if json_output:
        emit_json(payload)
    else:
        for p in payload["portfolios"]:
            click.echo(f"{p['id']} {p['status']} pid={p['pid']}")


def _mark_price_for_position(cache: DataCache, position: Position) -> float:
    latest = cache.get_latest_price(position.market_id)
    if latest is None:
        return float(position.avg_cost)
    if str(position.side).lower() == "no":
        return float(latest.no_price) if latest.no_price is not None else (1.0 - float(latest.yes_price))
    return float(latest.yes_price)


def _build_portfolio_compare_stats(cache: DataCache, portfolio: PaperPortfolio) -> dict:
    open_positions = cache.get_open_positions(portfolio.id)
    mtm_value = 0.0
    for pos in open_positions:
        mtm_value += float(pos.contracts) * _mark_price_for_position(cache, pos)

    portfolio_value = float(portfolio.cash_balance) + mtm_value
    unrealized_pnl = portfolio_value - float(portfolio.initial_cash)
    unrealized_pnl_pct = (unrealized_pnl / float(portfolio.initial_cash) * 100.0) if float(portfolio.initial_cash) else 0.0

    with get_session(cache._engine) as session:
        all_trade_rows = list(
            session.scalars(
                select(Trade).where(
                    and_(
                        Trade.portfolio_id == portfolio.id,
                        Trade.action.in_(["buy", "sell"]),
                    )
                )
            ).all()
        )
        sell_rows = [t for t in all_trade_rows if str(t.action).lower() == "sell"]
        winning_sells = [t for t in sell_rows if t.pnl is not None and float(t.pnl) > 0.0]
        avg_pnl = (
            sum(float(t.pnl or 0.0) for t in sell_rows) / len(sell_rows)
            if sell_rows
            else 0.0
        )

    return {
        "portfolio_id": portfolio.id,
        "strategy_path": portfolio.strategy_path,
        "status": portfolio.status,
        "started_at": portfolio.started_at,
        "initial_cash": float(portfolio.initial_cash),
        "portfolio_value": portfolio_value,
        "unrealized_pnl": unrealized_pnl,
        "unrealized_pnl_pct": round(unrealized_pnl_pct, 4),
        "total_trades": len(all_trade_rows),
        "win_rate": round((len(winning_sells) / len(sell_rows)), 4) if sell_rows else 0.0,
        "avg_pnl_per_trade": round(avg_pnl, 4),
        "open_positions": len(open_positions),
        "reload_count": int(portfolio.reload_count or 0),
    }


def _label_for_strategy(strategy_path: str, fallback_id: str) -> str:
    name = Path(strategy_path).stem if strategy_path else ""
    return name or fallback_id[:8]


@paper_group.command("compare")
@click.argument("portfolio_ids", nargs=-1)
@click.option("--all", "compare_all", is_flag=True)
@click.option("--json", "json_output", is_flag=True)
@json_errors
def paper_compare(portfolio_ids: tuple[str, ...], compare_all: bool, json_output: bool) -> None:
    ensure_initialized()
    cache = DataCache(get_engine())

    if compare_all:
        rows = [p for p in cache.list_paper_portfolios() if p.status == "running"]
    else:
        if len(portfolio_ids) != 2:
            raise click.UsageError("Usage: agenttrader paper compare <portfolio_id_1> <portfolio_id_2> [--json]")
        rows = []
        for pid in portfolio_ids:
            row = cache.get_portfolio(pid)
            if not row:
                raise AgentTraderError("NotFound", f"Portfolio not found: {pid}")
            rows.append(row)

    stats = [_build_portfolio_compare_stats(cache, row) for row in rows]

    payload = {"ok": True, "portfolios": stats}
    if json_output:
        emit_json(payload)
        return

    if not stats:
        click.echo("No running portfolios to compare")
        return

    table = Table(title="Paper Portfolio Comparison")
    table.add_column("")
    for item in stats:
        table.add_column(_label_for_strategy(item["strategy_path"], item["portfolio_id"]))

    metric_rows = [
        ("Strategy", lambda p: p["strategy_path"]),
        ("Status", lambda p: p["status"]),
        ("Started", lambda p: datetime.fromtimestamp(int(p["started_at"]), tz=UTC).strftime("%Y-%m-%d") if p["started_at"] else "-"),
        ("Initial Cash", lambda p: f"${p['initial_cash']:,.2f}"),
        ("Portfolio Value", lambda p: f"${p['portfolio_value']:,.2f}"),
        (
            "Unrealized PnL",
            lambda p: f"{'+' if p['unrealized_pnl'] >= 0 else ''}${p['unrealized_pnl']:,.2f} ({p['unrealized_pnl_pct']:.2f}%)",
        ),
        ("Total Trades", lambda p: str(p["total_trades"])),
        ("Win Rate", lambda p: f"{p['win_rate'] * 100:.1f}%"),
        ("Avg PnL per Trade", lambda p: f"${p['avg_pnl_per_trade']:,.2f}"),
        ("Open Positions", lambda p: str(p["open_positions"])),
        ("Reload Count", lambda p: str(p["reload_count"])),
    ]

    for label, fn in metric_rows:
        table.add_row(label, *[fn(item) for item in stats])
    Console().print(table)
