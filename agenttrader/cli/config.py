# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config
import click
import yaml

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
    click.echo("Next step: agenttrader sync --platform polymarket --days 7")


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
