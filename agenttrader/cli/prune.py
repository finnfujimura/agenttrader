# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import re
import time

import click
from sqlalchemy import delete, select

from agenttrader.cli.utils import emit_json, ensure_initialized, json_errors
from agenttrader.data.cache import DataCache
from agenttrader.data.orderbook_store import OrderBookStore
from agenttrader.db import get_engine, get_session
from agenttrader.db.schema import BacktestRun


def _parse_duration(value: str) -> int:
    m = re.fullmatch(r"(\d+)([dh])", value.strip().lower())
    if not m:
        raise click.ClickException("--older-than must be like 90d or 24h")
    num = int(m.group(1))
    unit = m.group(2)
    if unit == "d":
        return num * 24 * 3600
    return num * 3600


@click.command("prune")
@click.option("--older-than", required=True)
@click.option("--dry-run", is_flag=True)
@click.option("--json", "json_output", is_flag=True)
@json_errors
def prune_cmd(older_than: str, dry_run: bool, json_output: bool) -> None:
    ensure_initialized()
    seconds = _parse_duration(older_than)
    cutoff = int(time.time()) - seconds

    engine = get_engine()
    cache = DataCache(engine)
    ob_store = OrderBookStore()

    price_points_to_delete = cache.prune_price_history(cutoff, dry_run=dry_run)
    orderbook_files_to_delete = ob_store.prune(cutoff, dry_run=dry_run)

    with get_session(engine) as session:
        all_runs = list(session.scalars(select(BacktestRun).where(BacktestRun.completed_at.is_not(None)).order_by(BacktestRun.completed_at.desc())).all())
        keep_ids = {r.id for r in all_runs[:10]}
        delete_candidates = [r for r in all_runs if r.completed_at and r.completed_at < cutoff and r.id not in keep_ids]
        backtest_runs_to_delete = len(delete_candidates)
        if not dry_run and delete_candidates:
            session.execute(delete(BacktestRun).where(BacktestRun.id.in_([r.id for r in delete_candidates])))
            session.commit()

    payload = {
        "ok": True,
        "dry_run": dry_run,
        "price_points_to_delete": price_points_to_delete,
        "orderbook_files_to_delete": orderbook_files_to_delete,
        "backtest_runs_to_delete": backtest_runs_to_delete,
    }

    if json_output:
        emit_json(payload)
    else:
        click.echo(str(payload))
