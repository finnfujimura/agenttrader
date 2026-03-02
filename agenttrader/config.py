# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


APP_DIR = Path.home() / ".agenttrader"
CONFIG_PATH = APP_DIR / "config.yaml"
DB_PATH = APP_DIR / "db.sqlite"
ORDERBOOK_DIR = APP_DIR / "orderbooks"
RUNTIME_DIR = APP_DIR / "runtime"

DEFAULT_CONFIG: dict[str, Any] = {
    "schedule_interval_minutes": 15,
    "default_initial_cash": 10000.0,
    "sync_granularity": "hourly",
    "max_sync_days": 90,
    "paper_poll_interval_seconds": 5,
    "paper_persist_interval_seconds": 60,
    "paper_max_concurrent_requests": 8,
    "paper_history_buffer_hours": 24,
}


class ConfigError(RuntimeError):
    pass


def is_initialized() -> bool:
    return APP_DIR.exists() and CONFIG_PATH.exists() and DB_PATH.exists()


def ensure_app_dir() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    ORDERBOOK_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


def write_default_config() -> None:
    ensure_app_dir()
    CONFIG_PATH.write_text(yaml.safe_dump(DEFAULT_CONFIG, sort_keys=False), encoding="utf-8")


_VALID_GRANULARITIES = {"minute", "hourly", "daily"}


def _validate_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Validate and coerce config values. Raises ConfigError on invalid input."""
    errors = []

    # schedule_interval_minutes: int >= 1
    val = cfg.get("schedule_interval_minutes", 15)
    try:
        val = int(val)
        if val < 1:
            errors.append(f"schedule_interval_minutes must be >= 1, got {val}")
    except (TypeError, ValueError):
        errors.append(f"schedule_interval_minutes must be an integer, got {val!r}")
    cfg["schedule_interval_minutes"] = val

    # default_initial_cash: float, 1.0 – 1e9
    val = cfg.get("default_initial_cash", 10000.0)
    try:
        val = float(val)
        if not (1.0 <= val <= 1e9):
            errors.append(f"default_initial_cash must be between 1.0 and 1e9, got {val}")
    except (TypeError, ValueError):
        errors.append(f"default_initial_cash must be a number, got {val!r}")
    cfg["default_initial_cash"] = val

    # sync_granularity: one of minute/hourly/daily
    val = str(cfg.get("sync_granularity", "hourly"))
    if val not in _VALID_GRANULARITIES:
        errors.append(f"sync_granularity must be one of {_VALID_GRANULARITIES}, got {val!r}")
    cfg["sync_granularity"] = val

    # max_sync_days: int, 1 – 3650
    val = cfg.get("max_sync_days", 90)
    try:
        val = int(val)
        if not (1 <= val <= 3650):
            errors.append(f"max_sync_days must be between 1 and 3650, got {val}")
    except (TypeError, ValueError):
        errors.append(f"max_sync_days must be an integer, got {val!r}")
    cfg["max_sync_days"] = val

    # paper_poll_interval_seconds: int, 1 â€“ 60
    val = cfg.get("paper_poll_interval_seconds", 5)
    try:
        val = int(val)
        if not (1 <= val <= 60):
            errors.append(f"paper_poll_interval_seconds must be between 1 and 60, got {val}")
    except (TypeError, ValueError):
        errors.append(f"paper_poll_interval_seconds must be an integer, got {val!r}")
    cfg["paper_poll_interval_seconds"] = val

    # paper_persist_interval_seconds: int, 1 â€“ 3600
    val = cfg.get("paper_persist_interval_seconds", 60)
    try:
        val = int(val)
        if not (1 <= val <= 3600):
            errors.append(f"paper_persist_interval_seconds must be between 1 and 3600, got {val}")
    except (TypeError, ValueError):
        errors.append(f"paper_persist_interval_seconds must be an integer, got {val!r}")
    cfg["paper_persist_interval_seconds"] = val

    # paper_max_concurrent_requests: int, 1 â€“ 64
    val = cfg.get("paper_max_concurrent_requests", 8)
    try:
        val = int(val)
        if not (1 <= val <= 64):
            errors.append(f"paper_max_concurrent_requests must be between 1 and 64, got {val}")
    except (TypeError, ValueError):
        errors.append(f"paper_max_concurrent_requests must be an integer, got {val!r}")
    cfg["paper_max_concurrent_requests"] = val

    # paper_history_buffer_hours: int, 1 â€“ 720
    val = cfg.get("paper_history_buffer_hours", 24)
    try:
        val = int(val)
        if not (1 <= val <= 720):
            errors.append(f"paper_history_buffer_hours must be between 1 and 720, got {val}")
    except (TypeError, ValueError):
        errors.append(f"paper_history_buffer_hours must be an integer, got {val!r}")
    cfg["paper_history_buffer_hours"] = val

    if errors:
        raise ConfigError("Invalid config:\n" + "\n".join(f"  - {e}" for e in errors))
    return cfg


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise ConfigError("agenttrader not initialized. Run: agenttrader init")
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    merged = dict(DEFAULT_CONFIG)
    merged.update(data)
    return _validate_config(merged)


def save_config(data: dict[str, Any]) -> None:
    ensure_app_dir()
    CONFIG_PATH.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
