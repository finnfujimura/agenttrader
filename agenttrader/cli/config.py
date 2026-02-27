# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import sys
from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config
import click
import yaml

from agenttrader.cli.dataset import download_dataset
from agenttrader.config import APP_DIR, CONFIG_PATH, DB_PATH, ensure_app_dir, load_config, save_config, write_default_config
from agenttrader.cli.utils import ensure_initialized, json_errors


@click.command("init")
@json_errors
def init_cmd() -> None:
    """Initialize ~/.agenttrader/ directory and database."""
    ensure_app_dir()
    if not CONFIG_PATH.exists():
        write_default_config()
    elif not CONFIG_PATH.read_text(encoding="utf-8").strip():
        write_default_config()

    # Run Alembic migrations programmatically from package-local alembic.ini.
    package_dir = Path(__file__).resolve().parent.parent
    alembic_ini = package_dir / "db" / "alembic.ini"
    if not alembic_ini.exists():
        raise click.ClickException(f"Packaged alembic.ini not found at: {alembic_ini}")

    db_path = APP_DIR / "db.sqlite"
    alembic_cfg = Config(str(alembic_ini))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    alembic_command.upgrade(alembic_cfg, "head")

    # Ensure db file exists even if alembic wasn't available.
    DB_PATH.touch(exist_ok=True)
    click.echo(f"Initialized {APP_DIR}/")
    click.echo(f"Database: {db_path}")

    click.echo("\nHistorical dataset for backtesting")
    click.echo("─" * 40)
    click.echo("The Jon Becker dataset contains trade history for")
    click.echo("thousands of Polymarket and Kalshi markets (2021-present).")
    click.echo()
    click.echo("Download options:")
    click.echo("  [1] Full dataset (~36GB) — complete history, all markets")
    click.echo("  [2] Skip — download later with: agenttrader dataset download")
    click.echo()

    choice = "2"
    if sys.stdin.isatty():
        choice = click.prompt("Choice [1/2]", default="2")
    else:
        click.echo("Non-interactive shell detected. Skipping dataset prompt (default: 2).")

    if str(choice).strip() == "1":
        download_ok = download_dataset()
        if download_ok:
            click.echo()
            if sys.stdin.isatty():
                should_build = click.confirm(
                    "Build backtest index now? (recommended, ~5-10 minutes)",
                    default=True,
                )
            else:
                should_build = False
                click.echo("Non-interactive shell detected. Skipping index-build prompt.")
            if should_build:
                from agenttrader.data.index_builder import build_index

                result = build_index()
                if result.get("ok") and not result.get("skipped"):
                    stats = result.get("stats", {})
                    total = int(stats.get("polymarket_trades", 0)) + int(stats.get("kalshi_trades", 0))
                    click.echo(
                        f"Index built: {int(stats.get('markets_indexed', 0)):,} markets, {total:,} trades"
                    )
                elif result.get("skipped"):
                    click.echo("Index already exists.")
                else:
                    click.echo(f"Index build failed: {result.get('message')}")
                    click.echo("Run manually later: agenttrader dataset build-index")
    else:
        click.echo("Skipping dataset download.")
        click.echo("Run 'agenttrader dataset download' when ready.")
        click.echo("Until then, backtesting uses local sync cache data (SQLite fallback).")


@click.group("config")
def config_group() -> None:
    """Manage local agenttrader configuration."""


@config_group.command("set")
@click.argument("key")
@click.argument("value")
@json_errors
def config_set(key: str, value: str) -> None:
    ensure_initialized()
    cfg = load_config()

    # Preserve simple scalar types.
    if value.lower() in {"true", "false"}:
        parsed = value.lower() == "true"
    else:
        try:
            parsed = int(value)
        except ValueError:
            try:
                parsed = float(value)
            except ValueError:
                parsed = value

    cfg[key] = parsed
    save_config(cfg)
    _SENSITIVE_KEYS = {"pmxt_api_key", "api_key", "dome_api_key", "secret", "password", "token"}
    if key.lower() in _SENSITIVE_KEYS:
        click.echo(f"Set {key} = [redacted]")
    else:
        click.echo(str(parsed))


@config_group.command("get")
@click.argument("key")
@json_errors
def config_get(key: str) -> None:
    ensure_initialized()
    cfg = load_config()
    click.echo(str(cfg.get(key, "")))


@config_group.command("show")
@json_errors
def config_show() -> None:
    ensure_initialized()
    cfg = load_config()
    click.echo(yaml.safe_dump(cfg, sort_keys=False).strip())
