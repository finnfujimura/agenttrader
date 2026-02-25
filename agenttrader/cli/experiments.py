# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import json
import time
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table
from sqlalchemy import and_, select

from agenttrader.cli.utils import emit_json, ensure_initialized, json_errors
from agenttrader.config import APP_DIR, ensure_app_dir
from agenttrader.data.cache import DataCache
from agenttrader.db import get_engine, get_session
from agenttrader.db.schema import Position, Trade
from agenttrader.errors import AgentTraderError


METRIC_KEYS = ["total_return_pct", "sharpe_ratio", "max_drawdown_pct", "win_rate", "total_trades"]


@click.group("experiments")
def experiments_group() -> None:
    """Track strategy experiments across backtests and paper runs."""


def _experiments_path() -> Path:
    return APP_DIR / "experiments.json"


def _load_experiments_store() -> dict:
    path = _experiments_path()
    if not path.exists():
        return {"experiments": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "experiments" not in data:
        return {"experiments": []}
    if not isinstance(data["experiments"], list):
        return {"experiments": []}
    return data


def _save_experiments_store(store: dict) -> None:
    ensure_app_dir()
    _experiments_path().write_text(json.dumps(store, indent=2), encoding="utf-8")


def _parse_tags(tags: str) -> list[str]:
    return [t.strip() for t in str(tags).split(",") if t.strip()]


def _next_experiment_id(existing: set[str], ts: int) -> str:
    base = f"exp-{ts}"
    if base not in existing:
        return base
    index = 2
    while f"{base}-{index}" in existing:
        index += 1
    return f"{base}-{index}"


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_backtest_metrics(results_payload: dict | None) -> dict:
    metrics = (results_payload or {}).get("metrics", {}) if isinstance(results_payload, dict) else {}
    return {
        "total_return_pct": _to_float(metrics.get("total_return_pct")),
        "sharpe_ratio": _to_float(metrics.get("sharpe_ratio")),
        "max_drawdown_pct": _to_float(metrics.get("max_drawdown_pct")),
        "win_rate": _to_float(metrics.get("win_rate")),
        "total_trades": int(metrics.get("total_trades") or 0),
    }


def _extract_portfolio_metrics(cache: DataCache, portfolio_id: str, initial_cash: float) -> dict:
    open_positions = cache.get_open_positions(portfolio_id)
    mtm_value = 0.0
    for pos in open_positions:
        latest = cache.get_latest_price(pos.market_id)
        if latest is None:
            mark = float(pos.avg_cost)
        elif str(pos.side).lower() == "no":
            mark = float(latest.no_price) if latest.no_price is not None else (1.0 - float(latest.yes_price))
        else:
            mark = float(latest.yes_price)
        mtm_value += float(pos.contracts) * mark

    portfolio = cache.get_portfolio(portfolio_id)
    cash_balance = float(portfolio.cash_balance) if portfolio is not None else 0.0
    portfolio_value = cash_balance + mtm_value
    total_return_pct = ((portfolio_value - initial_cash) / initial_cash * 100.0) if initial_cash else 0.0

    with get_session(cache._engine) as session:
        trade_rows = list(
            session.scalars(
                select(Trade).where(
                    and_(
                        Trade.portfolio_id == portfolio_id,
                        Trade.action.in_(["buy", "sell"]),
                    )
                )
            ).all()
        )
    sells = [t for t in trade_rows if str(t.action).lower() == "sell"]
    winning = [t for t in sells if t.pnl is not None and float(t.pnl) > 0.0]
    win_rate = (len(winning) / len(sells)) if sells else 0.0

    return {
        "total_return_pct": round(total_return_pct, 4),
        "sharpe_ratio": None,
        "max_drawdown_pct": None,
        "win_rate": round(win_rate, 4),
        "total_trades": len(trade_rows),
    }


def _format_delta(metric_key: str, before, after) -> str | None:
    if before is None or after is None:
        return None
    delta = after - before
    if metric_key == "total_trades":
        return f"{int(round(delta)):+d}"
    text = f"{delta:+.4f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _find_experiment(store: dict, experiment_id: str) -> dict | None:
    for exp in store.get("experiments", []):
        if exp.get("id") == experiment_id:
            return exp
    return None


@experiments_group.command("log")
@click.argument("backtest_run_id", required=False)
@click.option("--portfolio", "portfolio_id", default=None)
@click.option("--note", default="")
@click.option("--tags", default="")
@click.option("--json", "json_output", is_flag=True)
@json_errors
def experiments_log(
    backtest_run_id: str | None,
    portfolio_id: str | None,
    note: str,
    tags: str,
    json_output: bool,
) -> None:
    ensure_initialized()
    if bool(backtest_run_id) == bool(portfolio_id):
        raise AgentTraderError(
            "UsageError",
            "Provide either <backtest_run_id> or --portfolio <portfolio_id>.",
        )

    cache = DataCache(get_engine())
    store = _load_experiments_store()
    experiments = store.get("experiments", [])
    now_ts = int(time.time())
    exp_id = _next_experiment_id({str(e.get("id")) for e in experiments}, now_ts)
    parsed_tags = _parse_tags(tags)

    if backtest_run_id:
        run = cache.get_backtest_run(backtest_run_id)
        if not run:
            raise AgentTraderError("NotFound", f"Backtest run not found: {backtest_run_id}")
        results = json.loads(run.results_json) if run.results_json else {}
        metrics = _extract_backtest_metrics(results)
        record = {
            "id": exp_id,
            "created_at": now_ts,
            "strategy_path": run.strategy_path,
            "strategy_hash": run.strategy_hash,
            "backtest_run_id": run.id,
            "paper_portfolio_id": None,
            "metrics": metrics,
            "note": note,
            "tags": parsed_tags,
        }
    else:
        portfolio = cache.get_portfolio(str(portfolio_id))
        if not portfolio:
            raise AgentTraderError("NotFound", f"Portfolio not found: {portfolio_id}")
        metrics = _extract_portfolio_metrics(cache, portfolio.id, float(portfolio.initial_cash))
        record = {
            "id": exp_id,
            "created_at": now_ts,
            "strategy_path": portfolio.strategy_path,
            "strategy_hash": portfolio.strategy_hash,
            "backtest_run_id": None,
            "paper_portfolio_id": portfolio.id,
            "metrics": metrics,
            "note": note,
            "tags": parsed_tags,
        }

    experiments.append(record)
    store["experiments"] = experiments
    _save_experiments_store(store)

    payload = {"ok": True, **record}
    if json_output:
        emit_json(payload)
    else:
        click.echo(f"Logged experiment {record['id']}")


@experiments_group.command("list")
@click.option("--json", "json_output", is_flag=True)
@json_errors
def experiments_list(json_output: bool) -> None:
    ensure_initialized()
    store = _load_experiments_store()
    experiments = sorted(store.get("experiments", []), key=lambda e: int(e.get("created_at", 0)), reverse=True)

    items = []
    for exp in experiments:
        metrics = exp.get("metrics", {}) or {}
        items.append(
            {
                "id": exp.get("id"),
                "created_at": exp.get("created_at"),
                "strategy_path": exp.get("strategy_path"),
                "backtest_run_id": exp.get("backtest_run_id"),
                "paper_portfolio_id": exp.get("paper_portfolio_id"),
                "sharpe_ratio": metrics.get("sharpe_ratio"),
                "total_return_pct": metrics.get("total_return_pct"),
                "max_drawdown_pct": metrics.get("max_drawdown_pct"),
                "note": exp.get("note", ""),
                "tags": exp.get("tags", []),
            }
        )

    payload = {"ok": True, "count": len(items), "experiments": items}
    if json_output:
        emit_json(payload)
        return

    table = Table(title="Experiments")
    table.add_column("ID")
    table.add_column("Created")
    table.add_column("Strategy")
    table.add_column("Return %")
    table.add_column("Sharpe")
    table.add_column("Max DD %")
    table.add_column("Note")
    for item in items:
        created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(item["created_at"] or 0)))
        table.add_row(
            str(item["id"]),
            created,
            str(Path(str(item["strategy_path"] or "")).name),
            str(item["total_return_pct"]),
            str(item["sharpe_ratio"]),
            str(item["max_drawdown_pct"]),
            str(item["note"] or "")[:60],
        )
    Console().print(table)


@experiments_group.command("note")
@click.argument("experiment_id")
@click.argument("note")
@click.option("--json", "json_output", is_flag=True)
@json_errors
def experiments_note(experiment_id: str, note: str, json_output: bool) -> None:
    ensure_initialized()
    store = _load_experiments_store()
    exp = _find_experiment(store, experiment_id)
    if not exp:
        raise AgentTraderError("NotFound", f"Experiment not found: {experiment_id}")
    exp["note"] = note
    _save_experiments_store(store)

    payload = {"ok": True, "id": experiment_id, "note": note}
    if json_output:
        emit_json(payload)
    else:
        click.echo(f"Updated note for {experiment_id}")


@experiments_group.command("compare")
@click.argument("experiment_id_1")
@click.argument("experiment_id_2")
@click.option("--json", "json_output", is_flag=True)
@json_errors
def experiments_compare(experiment_id_1: str, experiment_id_2: str, json_output: bool) -> None:
    ensure_initialized()
    store = _load_experiments_store()
    exp1 = _find_experiment(store, experiment_id_1)
    exp2 = _find_experiment(store, experiment_id_2)
    if not exp1:
        raise AgentTraderError("NotFound", f"Experiment not found: {experiment_id_1}")
    if not exp2:
        raise AgentTraderError("NotFound", f"Experiment not found: {experiment_id_2}")

    metrics1 = exp1.get("metrics", {}) or {}
    metrics2 = exp2.get("metrics", {}) or {}
    delta = {k: _format_delta(k, metrics1.get(k), metrics2.get(k)) for k in METRIC_KEYS}

    payload = {
        "ok": True,
        "experiments": [
            {"id": exp1.get("id"), "note": exp1.get("note", ""), "metrics": metrics1},
            {"id": exp2.get("id"), "note": exp2.get("note", ""), "metrics": metrics2},
        ],
        "delta": delta,
    }
    if json_output:
        emit_json(payload)
        return

    table = Table(title="Experiment Comparison")
    table.add_column("Metric")
    table.add_column(str(exp1.get("id")))
    table.add_column(str(exp2.get("id")))
    table.add_column("Delta")
    for key in METRIC_KEYS:
        table.add_row(
            key,
            str(metrics1.get(key)),
            str(metrics2.get(key)),
            str(delta.get(key)),
        )
    Console().print(table)


@experiments_group.command("show")
@click.argument("experiment_id")
@click.option("--json", "json_output", is_flag=True)
@json_errors
def experiments_show(experiment_id: str, json_output: bool) -> None:
    ensure_initialized()
    store = _load_experiments_store()
    exp = _find_experiment(store, experiment_id)
    if not exp:
        raise AgentTraderError("NotFound", f"Experiment not found: {experiment_id}")

    payload = {"ok": True, **exp}
    if json_output:
        emit_json(payload)
    else:
        click.echo(json.dumps(exp, indent=2))
