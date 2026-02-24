# DO NOT import dome_api_sdk here. Use agenttrader.data.dome_client only.
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


APP_DIR = Path.home() / ".agenttrader"
CONFIG_PATH = APP_DIR / "config.yaml"
DB_PATH = APP_DIR / "db.sqlite"
ORDERBOOK_DIR = APP_DIR / "orderbooks"

DEFAULT_CONFIG: dict[str, Any] = {
    "dome_api_key": "",
    "schedule_interval_minutes": 15,
    "default_initial_cash": 10000.0,
    "sync_granularity": "hourly",
    "max_sync_days": 90,
}


class ConfigError(RuntimeError):
    pass


def is_initialized() -> bool:
    return APP_DIR.exists() and CONFIG_PATH.exists() and DB_PATH.exists()


def ensure_app_dir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    ORDERBOOK_DIR.mkdir(parents=True, exist_ok=True)


def write_default_config() -> None:
    ensure_app_dir()
    CONFIG_PATH.write_text(yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False), encoding="utf-8")


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise ConfigError("agenttrader not initialized. Run: agenttrader init")
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)
    return merged


def save_config(data: dict[str, Any]) -> None:
    ensure_app_dir()
    CONFIG_PATH.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
