# Structure

## Repository Layout (Top Level)
- Core package code lives under `agenttrader/`.
- Project planning artifacts live under `.planning/` (including `.planning/codebase/`).
- Tests are organized under `tests/` with `tests/unit/`, `tests/integration/`, and `tests/fixtures/`.
- Root-level migration workspace exists in `alembic/` plus `alembic.ini`.
- Packaged migration/config assets also live inside `agenttrader/db/`.
- Utility scripts are in `scripts/`.
- User-facing docs are in `README.md`, `COMMANDS.md`, `SCHEMA.md`, and `docs/`.

## Package Substructure (`agenttrader/`)
- `agenttrader/cli/`: CLI command modules (`backtest.py`, `paper.py`, `sync.py`, `dataset.py`, etc.).
- `agenttrader/core/`: execution engine and runtime contexts (`backtest_engine.py`, `context.py`, `paper_daemon.py`).
- `agenttrader/data/`: source adapters, caches, models, and artifact stores.
- `agenttrader/db/`: SQLAlchemy schema/session setup, health checks, packaged Alembic migrations.
- `agenttrader/mcp/`: MCP server implementation (`server.py`).
- `agenttrader/dashboard/`: FastAPI dashboard server and static frontend (`static/index.html`, `static/app.js`).
- `agenttrader/config.py`: central path/config resolver for all runtimes.
- `agenttrader/errors.py` and `agenttrader/perf_logging.py`: shared infrastructure modules.

## Command Surface to File Mapping
- CLI root command group: `agenttrader/cli/main.py`.
- Init/config management: `agenttrader/cli/config.py`.
- Backtest commands: `agenttrader/cli/backtest.py`.
- Paper trading commands: `agenttrader/cli/paper.py`.
- Live sync commands: `agenttrader/cli/sync.py`.
- Dataset/index commands: `agenttrader/cli/dataset.py`.
- Market discovery/screening commands: `agenttrader/cli/markets.py`.
- Strategy validation command: `agenttrader/cli/validate.py`.
- Dashboard launcher: `agenttrader/cli/dashboard.py`.

## Runtime and Persistence Locations
- Path constants and root resolution: `agenttrader/config.py`.
- SQLite database schema: `agenttrader/db/schema.py`.
- DB engine/session construction: `agenttrader/db/__init__.py`.
- Orderbook files on disk: written by `agenttrader/data/orderbook_store.py`.
- Backtest artifact files: written by `agenttrader/data/backtest_artifacts.py`.
- Runtime daemon status files: managed in `agenttrader/core/paper_daemon.py`.
- Performance logs: emitted by `agenttrader/perf_logging.py`.

## Data Source Modules
- Provider contract: `agenttrader/data/provider.py`.
- Source selector and fallback policy: `agenttrader/data/source_selector.py`.
- DuckDB index reader: `agenttrader/data/index_adapter.py`.
- DuckDB index builder: `agenttrader/data/index_builder.py`.
- Raw parquet adapter: `agenttrader/data/parquet_adapter.py`.
- Live PMXT adapter: `agenttrader/data/pmxt_client.py`.
- SQLite cache gateway: `agenttrader/data/cache.py`.
- Unified market model dataclasses/enums: `agenttrader/data/models.py`.

## Naming and Organization Conventions
- Command modules are one file per command area under `agenttrader/cli/` (`paper.py`, `sync.py`, `prune.py`).
- Core runtime modules are behavior-focused (`*_engine.py`, `*_daemon.py`, `*_model.py`).
- Data adapters use explicit backend names (`parquet_adapter.py`, `index_adapter.py`, `pmxt_client.py`).
- Test files follow `test_<feature>.py` naming in `tests/unit/` and `tests/integration/`.
- Migration files are ordered numerically (`0001_*.py` through `0004_*.py`) in `agenttrader/db/migrations/versions/`.

## Testing Layout
- Cross-cutting fixtures and test setup: `tests/conftest.py` and `tests/fixtures/`.
- Unit coverage by subsystem (MCP, data, backtest, config, paper): `tests/unit/test_*.py`.
- End-to-end flow check: `tests/integration/test_full_workflow.py`.

## Notable Structural Details
- There are two migration trees: root `alembic/versions/` and packaged `agenttrader/db/migrations/versions/`; runtime init uses the packaged one via `agenttrader/cli/config.py`.
- There is a mirrored `agenttrader/db/data/` subtree; canonical imports throughout runtime code target `agenttrader/data/`.
- Temporary/runtime directories such as `venv/`, `.pytest_cache/`, and `__pycache__/` are present in the workspace but are not part of the core architecture.

