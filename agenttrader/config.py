# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml


_DEFAULT_APP_DIR = Path.home() / ".agenttrader"
DEFAULT_SHARED_DATA_ROOT = _DEFAULT_APP_DIR
PROJECT_PATHS_FILENAME = ".agenttrader-paths.json"


def _find_project_paths_file(start_dir: Path | None = None) -> Path | None:
    current = (start_dir or Path.cwd()).resolve()
    for base in (current, *current.parents):
        candidate = base / PROJECT_PATHS_FILENAME
        if candidate.exists():
            return candidate
    return None


def _load_project_path_overrides(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in payload.items()
        if isinstance(key, str) and isinstance(value, str) and str(value).strip()
    }


def _resolve_root(env_key: str, project_key: str, default: Path, project_overrides: dict[str, str]) -> Path:
    raw_value = os.environ.get(env_key)
    if raw_value:
        return Path(raw_value).expanduser()
    project_value = project_overrides.get(project_key)
    if project_value:
        return Path(project_value).expanduser()
    return default


PROJECT_PATHS_FILE: Path | None = None
PROJECT_PATH_OVERRIDES: dict[str, str] = {}
STATE_DIR: Path
DATA_ROOT: Path
APP_DIR: Path
CONFIG_PATH: Path
DB_PATH: Path
ORDERBOOK_DIR: Path
RUNTIME_DIR: Path
LOG_DIR: Path
ARTIFACTS_DIR: Path
SHARED_DATA_DIR: Path
BACKTEST_INDEX_PATH: Path


def reload_paths() -> None:
    global PROJECT_PATHS_FILE
    global PROJECT_PATH_OVERRIDES
    global STATE_DIR
    global DATA_ROOT
    global APP_DIR
    global CONFIG_PATH
    global DB_PATH
    global ORDERBOOK_DIR
    global RUNTIME_DIR
    global LOG_DIR
    global ARTIFACTS_DIR
    global SHARED_DATA_DIR
    global BACKTEST_INDEX_PATH

    PROJECT_PATHS_FILE = _find_project_paths_file()
    PROJECT_PATH_OVERRIDES = _load_project_path_overrides(PROJECT_PATHS_FILE)

    STATE_DIR = _resolve_root("AGENTTRADER_STATE_DIR", "state_dir", _DEFAULT_APP_DIR, PROJECT_PATH_OVERRIDES)
    DATA_ROOT = _resolve_root("AGENTTRADER_DATA_ROOT", "data_root", DEFAULT_SHARED_DATA_ROOT, PROJECT_PATH_OVERRIDES)

    # Legacy global install keeps the historical home-directory layout.
    if STATE_DIR.resolve() == _DEFAULT_APP_DIR.resolve():
        APP_DIR = STATE_DIR
        CONFIG_PATH = APP_DIR / "config.yaml"
        DB_PATH = APP_DIR / "db.sqlite"
    else:
        APP_DIR = STATE_DIR / ".agenttrader"
        CONFIG_PATH = APP_DIR / "config.yaml"
        DB_PATH = STATE_DIR / "db" / "db.sqlite"

    ORDERBOOK_DIR = APP_DIR / "orderbooks"
    RUNTIME_DIR = APP_DIR / "runtime"
    LOG_DIR = APP_DIR / "logs"
    ARTIFACTS_DIR = APP_DIR / "backtest_artifacts"
    SHARED_DATA_DIR = DATA_ROOT / "data"
    BACKTEST_INDEX_PATH = DATA_ROOT / "backtest_index.duckdb"


def write_project_paths_file(
    *,
    base_dir: Path | None = None,
    state_dir: Path | None = None,
    data_root: Path | None = None,
) -> Path:
    file_path = (base_dir or Path.cwd()).resolve() / PROJECT_PATHS_FILENAME
    payload: dict[str, str] = {}
    if state_dir is not None:
        payload["state_dir"] = str(Path(state_dir).resolve())
    if data_root is not None:
        payload["data_root"] = str(Path(data_root).resolve())
    file_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return file_path


reload_paths()

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
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    ORDERBOOK_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)


def ensure_data_root() -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    SHARED_DATA_DIR.mkdir(parents=True, exist_ok=True)


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
