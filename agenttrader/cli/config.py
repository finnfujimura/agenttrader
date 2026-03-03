# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import os
import sys
from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config
import click
import yaml

from agenttrader.cli import dataset as dataset_cli
import agenttrader.config as config_mod
from agenttrader.cli.utils import ensure_initialized, json_errors


@click.command("init")
@click.option(
    "--local-state",
    is_flag=True,
    default=False,
    help="Write a project-local path file (same as the default first-run behavior).",
)
@click.option(
    "--state-dir",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    help="Write a project-local path file that uses this state directory.",
)
@click.option(
    "--data-root",
    type=click.Path(path_type=Path, file_okay=False, dir_okay=True),
    help="Write a project-local path file that uses this shared data root.",
)
@click.option(
    "--global-state",
    is_flag=True,
    default=False,
    help="Use the legacy global state location instead of creating project-local ./.agenttrader state.",
)
@json_errors
def init_cmd(local_state: bool, state_dir: Path | None, data_root: Path | None, global_state: bool) -> None:
    """Initialize the configured agenttrader state directory and database."""
    if global_state and (local_state or state_dir is not None or data_root is not None):
        raise click.ClickException("--global-state cannot be combined with project-local path options.")

    should_write_project_paths = False
    chosen_state_dir = state_dir
    chosen_data_root = data_root

    if not global_state:
        if local_state or state_dir is not None or data_root is not None:
            should_write_project_paths = True
        elif (
            config_mod.PROJECT_PATHS_FILE is None
            and "AGENTTRADER_STATE_DIR" not in os.environ
            and "AGENTTRADER_DATA_ROOT" not in os.environ
        ):
            should_write_project_paths = True
            chosen_state_dir = Path.cwd()
            chosen_data_root = config_mod.DEFAULT_SHARED_DATA_ROOT

    if should_write_project_paths:
        if chosen_state_dir is None:
            chosen_state_dir = Path.cwd()
        if chosen_data_root is None:
            chosen_data_root = config_mod.DATA_ROOT
        project_file = config_mod.write_project_paths_file(
            base_dir=Path.cwd(),
            state_dir=chosen_state_dir,
            data_root=chosen_data_root,
        )
        config_mod.reload_paths()
        dataset_cli.DATA_DIR = config_mod.SHARED_DATA_DIR
        dataset_cli.BACKTEST_INDEX_PATH = config_mod.BACKTEST_INDEX_PATH
        click.echo(f"Project path config: {project_file}")

    config_mod.ensure_app_dir()
    if not config_mod.CONFIG_PATH.exists():
        config_mod.write_default_config()
    elif not config_mod.CONFIG_PATH.read_text(encoding="utf-8").strip():
        config_mod.write_default_config()

    # Run Alembic migrations programmatically from package-local alembic.ini.
    package_dir = Path(__file__).resolve().parent.parent
    alembic_ini = package_dir / "db" / "alembic.ini"
    if not alembic_ini.exists():
        raise click.ClickException(f"Packaged alembic.ini not found at: {alembic_ini}")

    db_path = config_mod.DB_PATH
    alembic_cfg = Config(str(alembic_ini))
    alembic_cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    alembic_command.upgrade(alembic_cfg, "head")

    # Ensure db file exists even if alembic wasn't available.
    config_mod.DB_PATH.touch(exist_ok=True)
    click.echo(f"Initialized {config_mod.APP_DIR}/")
    click.echo(f"Database: {db_path}")
    if config_mod.DATA_ROOT != config_mod.APP_DIR:
        click.echo(f"Shared data root: {config_mod.DATA_ROOT}")

    click.echo("\nHistorical dataset for backtesting")
    click.echo("-" * 40)
    click.echo("The Jon Becker dataset contains trade history for")
    click.echo("thousands of Polymarket and Kalshi markets (2021-present).")
    click.echo()
    click.echo("Download options:")
    click.echo("  [1] Full dataset (~36GB) -- complete history, all markets")
    click.echo("  [2] Skip -- download later with: agenttrader dataset download")
    click.echo()

    choice = "2"
    if sys.stdin.isatty():
        choice = click.prompt("Choice [1/2]", default="2")
    else:
        click.echo("Non-interactive shell detected. Skipping dataset prompt (default: 2).")

    if str(choice).strip() == "1":
        download_ok = dataset_cli.download_dataset()
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
    cfg = config_mod.load_config()

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
    config_mod.save_config(cfg)
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
    cfg = config_mod.load_config()
    click.echo(str(cfg.get(key, "")))


@config_group.command("show")
@json_errors
def config_show() -> None:
    ensure_initialized()
    cfg = config_mod.load_config()
    click.echo(yaml.safe_dump(cfg, sort_keys=False).strip())
