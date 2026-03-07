# Codebase Conventions

## Scope
- Conventions below are derived from active code in `agenttrader/` and test usage in `tests/`.
- Prioritize these over generic Python style guidance when planning edits.

## Hard Import Boundary
- Runtime modules frequently start with:
  - `# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.`
  - `from __future__ import annotations`
- Direct `pmxt` usage is intentionally centralized in `agenttrader/data/pmxt_client.py`.
- Keep new PMXT calls behind `agenttrader/data/pmxt_client.py`; do not import `pmxt` in `agenttrader/core/*`, `agenttrader/cli/*`, or `agenttrader/mcp/server.py`.

## Layering and Responsibilities
- `agenttrader/cli/*`: Click command surface and CLI UX (`agenttrader/cli/main.py`, `agenttrader/cli/utils.py`).
- `agenttrader/mcp/server.py`: MCP tool contracts, JSON payload shaping, sidecar/process guards.
- `agenttrader/core/*`: runtime/backtest/paper execution logic (`agenttrader/core/context.py`, `agenttrader/core/backtest_engine.py`).
- `agenttrader/data/*`: data adapters and source routing (`agenttrader/data/source_selector.py`, `agenttrader/data/parquet_adapter.py`, `agenttrader/data/cache.py`).
- `agenttrader/config.py`: all path/config defaults and environment overrides.

## Typing and Data Modeling
- Use Python 3.12+ typing syntax (`list[str]`, `dict[str, Any]`, `X | None`), as in `agenttrader/config.py`.
- Domain objects are dataclasses in `agenttrader/data/models.py` (`Market`, `PricePoint`, `OrderBook`, `Position`).
- String enums model persisted/API values (`ExecutionMode`, `Platform`, `MarketType` in `agenttrader/data/models.py`).
- New public/runtime functions should keep explicit return types where practical.

## Error and Payload Contract
- Domain-level errors use `AgentTraderError` variants in `agenttrader/errors.py`.
- CLI error shaping is centralized via `json_errors` in `agenttrader/cli/utils.py`.
- MCP responses are JSON-serializable dicts wrapped as text content (`_text`, `_error_payload` in `agenttrader/mcp/server.py`).
- Preserve stable keys in outward payloads: `ok`, `error`, `message`, optional `fix`, plus tool-specific fields.

## State and Path Handling
- Path resolution is centralized through `reload_paths()` and module globals in `agenttrader/config.py`.
- Respect existing env/project override keys:
  - `AGENTTRADER_STATE_DIR`
  - `AGENTTRADER_DATA_ROOT`
  - `AGENTTRADER_PERF_LOG_PATH`
- Use `ensure_app_dir()` / `ensure_data_root()` before writing files.

## Caching and Invalidation Pattern
- Module-level caches are used for expensive data source setup in `agenttrader/data/source_selector.py`.
- If behavior depends on refreshed data/files, call `invalidate_source_cache()` rather than rebuilding ad hoc.

## Naming and Structure
- Internal helpers are underscore-prefixed (`_bounded_int`, `_pid_alive`, `_resolve_root`).
- Constants are uppercase at module scope (`DEFAULT_CONFIG`, `PMXT_GUARDED_TOOLS`).
- Keep small utility helpers near call sites when they are module-private.

## Practical Edit Guardrails
- Keep the PMXT boundary comment and `from __future__ import annotations` in touched runtime modules.
- Reuse existing error contract patterns instead of introducing new payload shapes.
- Put new cross-cutting knobs in `agenttrader/config.py` first, then wire through callers.
- Follow existing layer boundaries instead of calling deep dependencies directly from CLI/MCP surfaces.
