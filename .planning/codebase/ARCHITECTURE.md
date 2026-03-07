# Architecture Map

## Scope
This repository is a Python prediction-market toolkit with three user-facing surfaces:
- CLI via `agenttrader` script (`agenttrader/cli/main.py`).
- MCP server for agent clients (`agenttrader/mcp/server.py`).
- Local dashboard API/UI (`agenttrader/dashboard/server.py` with `agenttrader/dashboard/static/`).

Core capabilities are strategy validation, market research, backtesting, and live paper trading.

## System Layers

### 1) Interface Layer
- CLI command router and command groups: `agenttrader/cli/main.py`.
- Command handlers:
  - setup/config: `agenttrader/cli/config.py`
  - dataset/index lifecycle: `agenttrader/cli/dataset.py`
  - sync/markets access: `agenttrader/cli/sync.py`, `agenttrader/cli/markets.py`
  - strategy workflows: `agenttrader/cli/validate.py`, `agenttrader/cli/backtest.py`, `agenttrader/cli/paper.py`
  - ops/observability: `agenttrader/cli/dashboard.py`, `agenttrader/cli/prune.py`, `agenttrader/cli/experiments.py`
- MCP API surface and tool contract: `agenttrader/mcp/server.py`.
- Dashboard HTTP API + static SPA serving: `agenttrader/dashboard/server.py`.

### 2) Strategy Runtime Layer
- Strategy contract (`BaseStrategy`) exposed to user strategy files: `agenttrader/core/base_strategy.py`.
- Execution contexts:
  - historical backtest context: `BacktestContext` in `agenttrader/core/context.py`
  - streaming backtest context: `StreamingBacktestContext` in `agenttrader/core/context.py`
  - live paper context: `LiveContext` in `agenttrader/core/context.py`
- Backtest orchestration engine (streaming index-first with fallback): `agenttrader/core/backtest_engine.py`.
- Live daemon lifecycle + hot reload: `agenttrader/core/paper_daemon.py` and runner `agenttrader/core/paper_daemon_runner.py`.
- Fill semantics:
  - orderbook-based model: `agenttrader/core/fill_model.py`
  - strict price-only model: `agenttrader/core/price_fill_model.py`

### 3) Data Access Layer
- Source selection and priority caching: `agenttrader/data/source_selector.py`.
- Provider protocol: `agenttrader/data/provider.py`.
- Backends:
  - normalized index + parquet metadata provider: `agenttrader/data/index_provider.py`
  - raw parquet adapter (DuckDB views): `agenttrader/data/parquet_adapter.py`
  - SQLite cache provider: `agenttrader/data/cache.py`, `agenttrader/data/cache_provider.py`
  - PMXT live API client: `agenttrader/data/pmxt_client.py`
  - historical orderbook file store: `agenttrader/data/orderbook_store.py`
  - backtest index reader/writer: `agenttrader/data/index_adapter.py`, `agenttrader/data/index_builder.py`
  - large result artifact storage: `agenttrader/data/backtest_artifacts.py`

### 4) Persistence and Configuration Layer
- Path/config resolution and app layout: `agenttrader/config.py`.
- DB engine/session setup: `agenttrader/db/__init__.py`.
- ORM schema: `agenttrader/db/schema.py`.
- Schema health checks: `agenttrader/db/health.py`.
- Migrations:
  - package migrations used by init: `agenttrader/db/migrations/versions/`
  - top-level Alembic tree also present: `alembic/versions/`

## Primary Runtime Flows

### Backtest Flow
1. Strategy file is validated (`agenttrader/cli/validate.py`).
2. Run metadata row is inserted into `backtest_runs` (`agenttrader/cli/backtest.py` + `agenttrader/db/schema.py`).
3. `BacktestEngine` executes with index-first strategy:
   - tries normalized index stream (`agenttrader/data/index_adapter.py`)
   - falls back to legacy parquet/cache mode if needed.
4. Metrics are written to DB; full curve/trades are moved to compressed artifact file via `agenttrader/data/backtest_artifacts.py`.

### Paper Trading Flow
1. `paper start` writes a `paper_portfolios` row (`agenttrader/cli/paper.py`).
2. Detached daemon process launches (`agenttrader/core/paper_daemon.py`).
3. `LiveContext` subscribes markets, polls PMXT live snapshots, persists selective data to SQLite/orderbook store (`agenttrader/core/context.py` + `agenttrader/data/cache.py` + `agenttrader/data/orderbook_store.py`).
4. Orders are simulated/filled and persisted to `positions`/`trades`.
5. Runtime heartbeat/status is written to `runtime` JSON files for status and MCP reads.

### MCP Tooling Flow
1. Tool registry and schema live in `agenttrader/mcp/server.py`.
2. Tool handlers reuse the same core modules as CLI (validation, backtest engine, cache/providers, daemon lifecycle).
3. Output contracts enforce structured `{ok, error, message, fix}` payload conventions for agent clients.

## Data Source Strategy (Important for Planning)
- Priority order is explicit: `normalized-index` -> `raw-parquet` -> `sqlite-cache` (`agenttrader/data/source_selector.py`).
- Implication: features should usually be implemented against provider interfaces and context methods, not against one backend directly.
- Backtest and research behavior can differ based on which source is available; this is a recurring testing concern.

## Guardrails and Cross-Cutting Concerns
- Strategy safety constraints are AST-enforced (forbidden imports, required `BaseStrategy` shape): `agenttrader/cli/validate.py`.
- Execution mode policy is centralized in `ExecutionMode` and context/fill-model branches:
  - `strict_price_only`
  - `observed_orderbook`
  - `synthetic_execution_model`
  (`agenttrader/data/models.py`, `agenttrader/core/context.py`, `agenttrader/core/backtest_engine.py`).
- Operational telemetry is centralized in JSONL perf logs: `agenttrader/perf_logging.py`.
- Shared error model for CLI/MCP surfaces: `agenttrader/errors.py`, `agenttrader/cli/utils.py`, `agenttrader/mcp/server.py`.

## Architecture Notes for Future Work
- The repository includes both top-level `alembic/` and package-local `agenttrader/db/migrations/`; `init` currently uses the package-local tree.
- Most feature work should touch interface + core + data layers together; test updates typically span both `tests/unit/` and `tests/integration/`.
