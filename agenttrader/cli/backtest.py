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
from agenttrader.data.backtest_artifacts import read_backtest_artifact, write_backtest_artifact
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


def _format_elapsed(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    total = max(int(seconds), 0)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _update_running_backtest_row(db_engine, run_id: str, payload: dict) -> None:
    with get_session(db_engine) as session:
        row = session.get(BacktestRun, run_id)
        if row:
            row.results_json = json.dumps(payload)
            session.commit()


def _make_cli_backtest_progress_callback(run_id: str, db_engine, json_output: bool):
    state = {"ok": True, "run_id": run_id, "status": "running"}

    def _emit(message: str) -> None:
        click.echo(message, err=json_output)

    def _callback(event: dict) -> None:
        kind = event.get("kind")
        if kind == "preflight":
            state["preflight"] = dict(event)
            parts = [
                "Backtest starting",
                f"source={event.get('data_source', '?')}",
                f"fidelity={event.get('fidelity', '?')}",
                f"markets={event.get('markets_tested', 0)}",
            ]
            if event.get("max_markets_applied") is not None:
                parts.append(f"max_markets={event['max_markets_applied']}")
            estimated = event.get("estimated_work_units")
            if estimated is not None:
                parts.append(f"estimated_{event.get('work_unit_label', 'events')}={estimated}")
            _emit(" | ".join(parts))
            for warning in event.get("warnings", []):
                _emit(f"Warning: {warning}")
            if event.get("large_run_warning"):
                _emit(f"Warning: {event['large_run_warning']}")
        elif kind == "progress":
            state["progress"] = dict(event)
            simulated_at = datetime.fromtimestamp(int(event["current_ts"]), tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            unit_label = event.get("work_unit_label", "events")
            _emit(
                "Backtest progress "
                f"{event.get('percent_complete', 0.0):5.1f}% | "
                f"sim={simulated_at} | "
                f"{event.get('processed_units', 0)} {unit_label} | "
                f"{event.get('throughput_per_second', 0.0)} {unit_label}/s | "
                f"elapsed={_format_elapsed(event.get('elapsed_seconds'))} | "
                f"eta={_format_elapsed(event.get('eta_seconds'))}"
            )
        _update_running_backtest_row(db_engine, run_id, state)

    return _callback


def _backtest_run(strategy_path: str, args: list[str]) -> None:
    ensure_initialized()

    from_date = None
    to_date = None
    initial_cash = None
    max_markets = None
    fidelity = "exact_trade"
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
        if token == "--max-markets" and i + 1 < len(args):
            max_markets = int(args[i + 1])
            i += 2
            continue
        if token == "--fidelity" and i + 1 < len(args):
            fidelity = str(args[i + 1]).strip()
            if fidelity not in {"exact_trade", "bar_1h", "bar_1d"}:
                raise click.UsageError("--fidelity must be one of: exact_trade, bar_1h, bar_1d")
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
    initial_cash = float(initial_cash if initial_cash is not None else cfg["default_initial_cash"])

    run_id = str(uuid.uuid4())
    strategy_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    now_ts = int(datetime.now(tz=UTC).timestamp())

    db_engine = get_engine()
    progress_callback = _make_cli_backtest_progress_callback(run_id, db_engine, json_output)
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
                schedule_interval_minutes=cfg["schedule_interval_minutes"],
                max_markets=max_markets,
                fidelity=fidelity,
            ),
            progress_callback=progress_callback,
        )
        if results.get("ok") is False:
            raise AgentTraderError(results.get("error", "BacktestError"), results.get("message", "Backtest failed"), results)
        artifact_payload = results.pop("_artifact_payload", None)
        if artifact_payload is not None:
            equity_curve = artifact_payload.get("equity_curve", [])
            trades = artifact_payload.get("trades", [])
        else:
            equity_curve = results.pop("equity_curve", [])
            trades = results.pop("trades", [])
        results["artifact_path"] = write_backtest_artifact(run_id, equity_curve, trades)
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
            if results.get("data_source") == "normalized-index":
                click.echo("Data source: normalized backtest index (DuckDB)")
            elif results.get("data_source") == "parquet":
                click.echo("Data source: Jon Becker dataset (parquet) -- 2021-present")
            else:
                click.echo("Data source: local sync cache (SQLite) -- run 'agenttrader dataset download' for full history")
            click.echo(f"Final value: {results['final_value']:.2f}")
            click.echo(f"Sharpe: {results['metrics']['sharpe_ratio']}")
            for warning in results.get("warnings", []):
                click.echo(f"Warning: {warning}")
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
    runs = []
    for r in rows:
        progress = None
        if r.status == "running" and r.results_json:
            try:
                progress = json.loads(r.results_json).get("progress")
            except json.JSONDecodeError:
                progress = None
        runs.append(
            {
                "id": r.id,
                "strategy_path": r.strategy_path,
                "start_date": r.start_date,
                "end_date": r.end_date,
                "status": r.status,
                "created_at": r.created_at,
                "completed_at": r.completed_at,
                "progress_pct": (progress or {}).get("percent_complete"),
                "processed_units": (progress or {}).get("processed_units"),
                "work_unit_label": (progress or {}).get("work_unit_label"),
            }
        )
    payload = {
        "ok": True,
        "runs": runs,
    }
    if json_output:
        emit_json(payload)
        return

    table = Table(title="Backtest Runs")
    table.add_column("ID")
    table.add_column("Strategy")
    table.add_column("Range")
    table.add_column("Status")
    table.add_column("Progress")
    for row in payload["runs"]:
        progress = ""
        if row.get("progress_pct") is not None:
            progress = f"{row['progress_pct']:.1f}%"
            if row.get("processed_units") is not None:
                progress += f" ({row['processed_units']} {row.get('work_unit_label', 'events')})"
        table.add_row(
            row["id"][:8],
            Path(row["strategy_path"]).name,
            f"{row['start_date']} -> {row['end_date']}",
            row["status"],
            progress,
        )
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

    if row.results_json:
        data = json.loads(row.results_json)
        if row.status == "complete":
            artifact = read_backtest_artifact(run_id)
            if artifact.get("equity_curve") or "equity_curve" not in data:
                data["equity_curve"] = artifact.get("equity_curve", [])
            if artifact.get("trades") or "trades" not in data:
                data["trades"] = artifact.get("trades", [])
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
            for warning in data.get("warnings", []):
                click.echo(f"Warning: {warning}")
        elif row.status == "running" and data.get("progress"):
            progress = data["progress"]
            click.echo(
                f"Progress: {progress.get('percent_complete', 0.0):.1f}% | "
                f"{progress.get('processed_units', 0)} {progress.get('work_unit_label', 'events')} | "
                f"ETA { _format_elapsed(progress.get('eta_seconds')) }"
            )
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
