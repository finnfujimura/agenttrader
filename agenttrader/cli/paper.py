# DO NOT import dome_api_sdk here. Use agenttrader.data.dome_client only.
from __future__ import annotations

import hashlib
import os
import signal
import uuid
from datetime import UTC, datetime
from pathlib import Path

import click
from sqlalchemy import select

from agenttrader.cli.utils import emit_json, ensure_initialized, json_errors
from agenttrader.cli.validate import validate_strategy_file
from agenttrader.config import load_config
from agenttrader.core.paper_daemon import PaperDaemon
from agenttrader.data.cache import DataCache
from agenttrader.db import get_engine, get_session
from agenttrader.db.schema import PaperPortfolio
from agenttrader.errors import AgentTraderError


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
    initial_cash = float(initial_cash if initial_cash is not None else cfg.get("default_initial_cash", 10000.0))

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

    pid = daemon.start_as_daemon()
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
                    os.kill(int(row.pid), signal.SIGTERM)
                except ProcessLookupError:
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
    positions = []
    unrealized_total = 0.0
    for pos in positions_rows:
        latest = cache.get_latest_price(pos.market_id)
        current_price = latest.yes_price if latest else pos.avg_cost
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
    payload = {
        "ok": True,
        "portfolios": [
            {
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
            }
            for p in rows
        ],
    }
    if json_output:
        emit_json(payload)
    else:
        for p in payload["portfolios"]:
            click.echo(f"{p['id']} {p['status']} pid={p['pid']}")
