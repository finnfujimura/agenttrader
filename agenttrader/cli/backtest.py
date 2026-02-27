# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import sys
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from agenttrader.cli.utils import emit_json, ensure_initialized, json_errors
from agenttrader.cli.validate import validate_strategy_file
from agenttrader.config import load_config
from agenttrader.core.backtest_engine import BacktestConfig, BacktestEngine
from agenttrader.core.base_strategy import BaseStrategy
from agenttrader.data.cache import DataCache
from agenttrader.data.orderbook_store import OrderBookStore
from agenttrader.data.parquet_adapter import ParquetDataAdapter
from agenttrader.db import get_engine, get_session
from agenttrader.db.schema import BacktestRun
from agenttrader.errors import AgentTraderError


@click.command(
    "backtest",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.argument("target")
@click.argument("rest", nargs=-1)
@json_errors
def backtest_cmd(target: str, rest: tuple[str, ...]) -> None:
    if target == "list":
        _backtest_list(list(rest))
        return
    if target == "show":
        _backtest_show(list(rest))
        return

    _backtest_run(target, list(rest))


def get_backtest_engine() -> BacktestEngine:
    """Return backtest engine with best available data source."""
    adapter = ParquetDataAdapter()
    if adapter.is_available():
        return BacktestEngine(data_source=adapter)
    db_engine = get_engine()
    return BacktestEngine(
        data_source=DataCache(db_engine),
        orderbook_store=OrderBookStore(),
    )


def _backtest_run(strategy_path: str, args: list[str]) -> None:
    ensure_initialized()

    from_date = None
    to_date = None
    initial_cash = None
    json_output = False
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--from" and i + 1 < len(args):
            from_date = args[i + 1]
            i += 2
            continue
        if token == "--to" and i + 1 < len(args):
            to_date = args[i + 1]
            i += 2
            continue
        if token == "--cash" and i + 1 < len(args):
            initial_cash = float(args[i + 1])
            i += 2
            continue
        if token == "--json":
            json_output = True
            i += 1
            continue
        raise click.UsageError(f"Unknown option for backtest run: {token}")

    path = Path(strategy_path)
    if not path.exists():
        payload = {
            "ok": False,
            "error": "FileNotFoundError",
            "message": f"Strategy file not found: {strategy_path}",
        }
        if json_output:
            emit_json(payload)
            raise click.exceptions.Exit(1)
        raise AgentTraderError("FileNotFoundError", payload["message"])

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
    from_date = from_date or datetime.now(tz=UTC).strftime("%Y-01-01")
    to_date = to_date or datetime.now(tz=UTC).strftime("%Y-%m-%d")
    initial_cash = float(initial_cash if initial_cash is not None else cfg.get("default_initial_cash", 10000.0))

    run_id = str(uuid.uuid4())
    strategy_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    now_ts = int(datetime.now(tz=UTC).timestamp())

    db_engine = get_engine()
    with get_session(db_engine) as session:
        session.add(
            BacktestRun(
                id=run_id,
                strategy_path=str(path),
                strategy_hash=strategy_hash,
                start_date=from_date,
                end_date=to_date,
                initial_cash=initial_cash,
                status="running",
                error=None,
                results_json=None,
                created_at=now_ts,
                completed_at=None,
            )
        )
        session.commit()

    try:
        module = _import_strategy_module(path)
        strategy_class = _find_strategy_class(module)
        engine = get_backtest_engine()
        results = engine.run(
            strategy_class,
            BacktestConfig(
                strategy_path=str(path),
                start_date=from_date,
                end_date=to_date,
                initial_cash=initial_cash,
                schedule_interval_minutes=int(cfg.get("schedule_interval_minutes", 15)),
            ),
        )
        results["run_id"] = run_id
        results["status"] = "complete"

        with get_session(db_engine) as session:
            row = session.get(BacktestRun, run_id)
            if row:
                row.status = "complete"
                row.results_json = json.dumps(results)
                row.completed_at = int(datetime.now(tz=UTC).timestamp())
                session.commit()

        if json_output:
            emit_json(results)
        else:
            click.echo(f"Backtest complete: {run_id}")
            if results.get("data_source") == "parquet":
                click.echo("Data source: Jon Becker dataset (parquet) — 2021-present")
            else:
                click.echo("Data source: local sync cache (SQLite) — run 'agenttrader dataset download' for full history")
            click.echo(f"Final value: {results['final_value']:.2f}")
            click.echo(f"Sharpe: {results['metrics']['sharpe_ratio']}")
    except Exception as exc:
        tb = traceback.format_exc()
        with get_session(db_engine) as session:
            row = session.get(BacktestRun, run_id)
            if row:
                row.status = "failed"
                row.error = tb
                row.completed_at = int(datetime.now(tz=UTC).timestamp())
                session.commit()

        file = str(path)
        line = 1
        extracted = traceback.extract_tb(exc.__traceback__)
        if extracted:
            last = extracted[-1]
            file = last.filename
            line = last.lineno

        payload = {
            "ok": False,
            "error": "StrategyError",
            "message": str(exc),
            "file": file,
            "line": line,
            "traceback": tb,
        }
        if json_output:
            emit_json(payload)
            raise click.exceptions.Exit(1)
        raise AgentTraderError("StrategyError", str(exc), payload)


def _backtest_list(args: list[str]) -> None:
    ensure_initialized()
    json_output = False
    if args:
        if args == ["--json"]:
            json_output = True
        else:
            raise click.UsageError("Usage: agenttrader backtest list [--json]")

    cache = DataCache(get_engine())
    rows = cache.list_backtest_runs(limit=200)
    payload = {
        "ok": True,
        "runs": [
            {
                "id": r.id,
                "strategy_path": r.strategy_path,
                "start_date": r.start_date,
                "end_date": r.end_date,
                "status": r.status,
                "created_at": r.created_at,
                "completed_at": r.completed_at,
            }
            for r in rows
        ],
    }
    if json_output:
        emit_json(payload)
        return

    table = Table(title="Backtest Runs")
    table.add_column("ID")
    table.add_column("Strategy")
    table.add_column("Range")
    table.add_column("Status")
    for row in payload["runs"]:
        table.add_row(row["id"][:8], Path(row["strategy_path"]).name, f"{row['start_date']} -> {row['end_date']}", row["status"])
    Console().print(table)


def _backtest_show(args: list[str]) -> None:
    ensure_initialized()
    if not args:
        raise click.UsageError("Usage: agenttrader backtest show <run_id> [--json]")
    run_id = args[0]
    json_output = args[1:] == ["--json"]
    if args[1:] and not json_output:
        raise click.UsageError("Usage: agenttrader backtest show <run_id> [--json]")

    cache = DataCache(get_engine())
    row = cache.get_backtest_run(run_id)
    if not row:
        raise AgentTraderError("NotFound", f"Backtest run not found: {run_id}")

    if row.status == "complete" and row.results_json:
        data = json.loads(row.results_json)
    else:
        data = {
            "ok": True,
            "run_id": row.id,
            "strategy_path": row.strategy_path,
            "start_date": row.start_date,
            "end_date": row.end_date,
            "initial_cash": row.initial_cash,
            "status": row.status,
            "error": row.error,
        }

    if json_output:
        emit_json(data)
    else:
        click.echo(f"Run {row.id} - {row.status}")
        if row.status == "complete":
            click.echo(f"Final value: {data.get('final_value', 0):.2f}")
            click.echo(f"Sharpe: {data.get('metrics', {}).get('sharpe_ratio', 0)}")
        elif row.error:
            click.echo(row.error)


def _import_strategy_module(path: Path):
    spec = importlib.util.spec_from_file_location("user_strategy", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load strategy from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        del sys.modules[spec.name]
        raise
    return module


def _find_strategy_class(module):
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if issubclass(cls, BaseStrategy) and cls is not BaseStrategy:
            return cls
    raise RuntimeError("No BaseStrategy subclass found in strategy file")
