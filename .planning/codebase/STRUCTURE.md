# Repository Structure

## Top-Level Layout
- `agenttrader/` - main Python package (CLI, core runtime, data layer, DB, MCP server, dashboard).
- `tests/` - unit/integration tests plus fixtures.
- `alembic/` - top-level migration tree.
- `.planning/` - project planning artifacts (requirements, roadmap, phase docs, codebase maps).
- `.github/workflows/` - release/publish automation (`publish.yml`).
- `scripts/` - ad hoc helper/testing scripts.
- Root docs/manifests: `README.md`, `COMMANDS.md`, `SCHEMA.md`, `pyproject.toml`, `alembic.ini`.

## Package Map (`agenttrader/`)

### Public Package Root
- `agenttrader/__init__.py` - exports `BaseStrategy` and package version.
- `agenttrader/config.py` - path/config resolution and app storage layout.
- `agenttrader/errors.py` - typed error payload model used across CLI/MCP.
- `agenttrader/perf_logging.py` - JSONL performance logging.

### CLI Surface (`agenttrader/cli/`)
- `main.py` - command registration and MCP startup command.
- `config.py` - `init` + config management.
- `dataset.py` - dataset download/verify/index build.
- `sync.py` - live PMXT sync into local cache.
- `markets.py` - market list/price/history/screen/match commands.
- `validate.py` - strategy AST validation.
- `backtest.py` - backtest run/list/show lifecycle.
- `paper.py` - daemon start/stop/status/list/compare.
- `experiments.py` - experiment tracking/comparison.
- `dashboard.py` - local API/UI server startup.
- `prune.py` - retention cleanup.
- `utils.py` - shared CLI wrappers (`json_errors`, init guard, JSON output).

### Core Runtime (`agenttrader/core/`)
- `base_strategy.py` - strategy interface and lifecycle hooks.
- `context.py` - execution contexts:
  - `BacktestContext`
  - `StreamingBacktestContext`
  - `LiveContext`
- `backtest_engine.py` - backtest orchestration and metric computation.
- `paper_daemon.py` - live daemon loop, hot reload, runtime status.
- `paper_daemon_runner.py` - process entrypoint for detached daemon.
- `fill_model.py` / `price_fill_model.py` - order fill logic.
- `scheduler.py` - schedule helper.

### Data Layer (`agenttrader/data/`)
- `models.py` - core enums/dataclasses (`Market`, `PricePoint`, `OrderBook`, `ExecutionMode`).
- `source_selector.py` - source discovery and priority selection.
- `provider.py` - provider protocol contract.
- `index_provider.py` / `cache_provider.py` - provider implementations.
- `parquet_adapter.py` - DuckDB adapter over parquet dataset.
- `index_adapter.py` - read from normalized DuckDB backtest index.
- `index_builder.py` - build normalized index from raw parquet.
- `cache.py` - SQLite-backed cache and portfolio/backtest CRUD.
- `pmxt_client.py` - live PMXT integration.
- `orderbook_store.py` - compressed file-backed orderbook snapshots.
- `backtest_artifacts.py` - compressed backtest curve/trade artifacts.

### Database Layer (`agenttrader/db/`)
- `__init__.py` - SQLAlchemy engine/session helpers.
- `schema.py` - ORM table models.
- `health.py` - schema health checks.
- `alembic.ini` - package-local migration config.
- `migrations/env.py` and `migrations/versions/*.py` - migration scripts.

### MCP and Dashboard
- `agenttrader/mcp/server.py` - MCP tool definitions and handlers.
- `agenttrader/dashboard/server.py` - FastAPI endpoints.
- `agenttrader/dashboard/static/index.html`
- `agenttrader/dashboard/static/app.js`

## Tests (`tests/`)
- `tests/unit/` - granular behavior and regression tests (engine, MCP, source routing, paper trading, validation).
- `tests/integration/test_full_workflow.py` - end-to-end CLI workflow test.
- `tests/fixtures/` - sample market/orderbook/strategy artifacts.
- `tests/conftest.py` - shared fixtures and environment stubs.

## Planning and Process Docs (`.planning/`)
- project-level docs: `.planning/PROJECT.md`, `.planning/REQUIREMENTS.md`, `.planning/ROADMAP.md`, `.planning/STATE.md`.
- codebase maps: `.planning/codebase/`.
- phase docs: `.planning/phases/`.
- research docs: `.planning/research/`.

## Build/Release and Packaging
- `pyproject.toml` - package metadata, dependencies, CLI script entrypoint.
- `.github/workflows/publish.yml` - build and publish to PyPI on tags/releases.
- `MANIFEST.in` - package inclusion controls.

## Practical Entry Points
- CLI executable target: `agenttrader.cli.main:cli` (declared in `pyproject.toml`).
- MCP process target: `agenttrader mcp` -> `agenttrader/mcp/server.py`.
- Dashboard run path: `agenttrader dashboard` -> `agenttrader/dashboard/server.py`.
- Local init and migrations: `agenttrader init` -> `agenttrader/cli/config.py`.
