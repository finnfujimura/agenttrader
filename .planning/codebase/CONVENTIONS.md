# Code Quality Conventions

## Scope
- This document captures conventions observed in the current repository, not generic Python style rules.
- Source examples are taken from `agenttrader/config.py`, `agenttrader/cli/utils.py`, `agenttrader/mcp/server.py`, `agenttrader/core/backtest_engine.py`, `agenttrader/core/context.py`, `agenttrader/data/source_selector.py`, and `agenttrader/errors.py`.

## Language and typing style
- Target runtime is modern Python (`requires-python >=3.12`) in `pyproject.toml`.
- Modules frequently use postponed evaluation with `from __future__ import annotations` (for example `agenttrader/config.py`, `agenttrader/cli/backtest.py`, `agenttrader/core/backtest_engine.py`).
- Type hints use built-in generics and PEP 604 unions (`list[str]`, `Path | None`) across `agenttrader/config.py` and `agenttrader/mcp/server.py`.
- Domain shapes are modeled with `@dataclass` and `Enum` in `agenttrader/data/models.py` and `agenttrader/errors.py`.

## Naming and file-level patterns
- Functions and variables are snake_case (`reload_paths`, `_build_progress_payload`) across `agenttrader/config.py` and `agenttrader/core/backtest_engine.py`.
- Classes are PascalCase (`BacktestEngine`, `StrategyValidator`, `AgentTraderError`) in `agenttrader/core/backtest_engine.py`, `agenttrader/cli/validate.py`, and `agenttrader/errors.py`.
- Constants are uppercase with underscores (`DEFAULT_FIXES`, `PROGRESS_INTERVAL_SECONDS`, `REQUIRED_COLUMNS`) in `agenttrader/mcp/server.py`, `agenttrader/core/backtest_engine.py`, and `agenttrader/db/health.py`.
- Internal/private helpers consistently use a leading underscore (`_error_payload`, `_ensure_pmxt_sidecar_safe`, `_validate_config`) in `agenttrader/mcp/server.py` and `agenttrader/config.py`.

## Architecture boundary conventions
- Many modules include an explicit guard comment: `DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.` (seen in `agenttrader/cli/main.py`, `agenttrader/config.py`, `agenttrader/mcp/server.py`, and others).
- Keep PMXT SDK integration concentrated in `agenttrader/data/pmxt_client.py`; other layers call through adapters or MCP functions.
- Data source routing follows a strict fallback chain in `agenttrader/data/source_selector.py`: normalized index -> raw parquet -> sqlite cache.

## API and payload conventions
- CLI JSON responses use a stable shape via `emit_json()` in `agenttrader/cli/utils.py` and command handlers in `agenttrader/cli/backtest.py`.
- MCP tool responses use dictionary payloads with `ok`, `error`, `message`, and often `fix` via `_error_payload()` in `agenttrader/mcp/server.py`.
- Error payloads are action-oriented; remediation hints are centralized in `DEFAULT_FIXES` in `agenttrader/mcp/server.py`.
- Backtest and portfolio flows prefer structured result objects over exceptions at boundaries (for example `agenttrader/core/backtest_engine.py` and `agenttrader/mcp/server.py`).

## Error handling conventions
- Domain errors use `AgentTraderError` with machine-readable fields (`error`, `message`, optional `fix`, optional `extra`) in `agenttrader/errors.py`.
- CLI commands use the `@json_errors` decorator in `agenttrader/cli/utils.py` to normalize exception behavior between text mode and `--json`.
- Validation collects multiple errors before failing, rather than failing fast on first issue (`_validate_config()` in `agenttrader/config.py`, `StrategyValidator` in `agenttrader/cli/validate.py`).
- Infrastructure edges often catch broad exceptions and degrade gracefully with warnings/logging (`agenttrader/data/source_selector.py`, `agenttrader/perf_logging.py`, selected sections of `agenttrader/mcp/server.py`).
- Programmer/input errors are explicit `ValueError`/`RuntimeError` in lower layers (`agenttrader/data/parquet_adapter.py`, `agenttrader/core/backtest_engine.py`), then translated at command or MCP boundaries.

## Persistence and side-effect conventions
- SQLite engine/session objects are cached process-wide in `agenttrader/db/__init__.py` for reuse and predictable behavior.
- SQLite setup applies WAL mode via SQLAlchemy connect event in `agenttrader/db/__init__.py`.
- Paths and environment overrides are resolved centrally in `agenttrader/config.py`; callers rely on `reload_paths()`, `ensure_app_dir()`, and `ensure_data_root()`.
- Performance logging is best-effort and non-fatal (`agenttrader/perf_logging.py` intentionally swallows logging exceptions).

## Practical guidance for new code
- Keep command/MCP boundaries returning structured payloads compatible with patterns in `agenttrader/cli/utils.py` and `agenttrader/mcp/server.py`.
- Add typed interfaces and dataclasses for new domain entities following `agenttrader/data/models.py`.
- Preserve source selection and PMXT isolation boundaries (`agenttrader/data/source_selector.py` and `agenttrader/data/pmxt_client.py`).
- When introducing new errors, include user-facing fixes consistent with `agenttrader/errors.py` and `agenttrader/mcp/server.py`.
- If you must catch `Exception`, log or annotate context and return a deterministic fallback, matching existing behavior in `agenttrader/data/source_selector.py` and `agenttrader/perf_logging.py`.
