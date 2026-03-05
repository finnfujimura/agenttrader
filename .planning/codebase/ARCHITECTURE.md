# Architecture

## System Overview
`agenttrader` is a Python application that exposes the same trading engine through three front doors: CLI, MCP server, and dashboard API.
The package entrypoint is wired in `pyproject.toml` (`agenttrader = agenttrader.cli.main:cli`).
Core runtime surfaces are implemented in `agenttrader/cli/main.py`, `agenttrader/mcp/server.py`, and `agenttrader/dashboard/server.py`.
The strategy contract is a single abstract base class in `agenttrader/core/base_strategy.py`.

## Primary Entry Points
- CLI process starts at `agenttrader/cli/main.py`, which registers command groups from `agenttrader/cli/*.py`.
- MCP stdio server starts from the `mcp` subcommand in `agenttrader/cli/main.py`, then runs `agenttrader/mcp/server.py:main`.
- Dashboard HTTP process starts in `agenttrader/cli/dashboard.py` and serves `agenttrader/dashboard/server.py`.
- Paper daemon subprocess starts with `agenttrader/core/paper_daemon_runner.py` and executes `agenttrader/core/paper_daemon.py`.

## Layered Design
- Surface layer: transport and command adapters in `agenttrader/cli/*`, `agenttrader/mcp/server.py`, `agenttrader/dashboard/server.py`.
- Orchestration layer: workflow sequencing in `agenttrader/core/backtest_engine.py` and `agenttrader/core/paper_daemon.py`.
- Strategy boundary layer: runtime API and lifecycle hooks in `agenttrader/core/base_strategy.py` and `agenttrader/core/context.py`.
- Data-access layer: source adapters and selection in `agenttrader/data/source_selector.py`, `agenttrader/data/index_provider.py`, `agenttrader/data/parquet_adapter.py`, `agenttrader/data/cache.py`, `agenttrader/data/pmxt_client.py`.
- Persistence layer: SQLAlchemy schema/session utilities in `agenttrader/db/schema.py` and `agenttrader/db/__init__.py`; binary artifacts in `agenttrader/data/orderbook_store.py` and `agenttrader/data/backtest_artifacts.py`.

## Core Architectural Patterns
- Dependency inversion by protocol/interface: `agenttrader/data/provider.py` defines a provider contract; callers use provider-shaped objects rather than raw backends.
- Multi-source routing with fallback: `agenttrader/data/source_selector.py` chooses `normalized-index -> raw-parquet -> sqlite-cache`.
- Shared strategy API across modes: `ExecutionContext` in `agenttrader/core/context.py` is implemented by `BacktestContext`, `StreamingBacktestContext`, and `LiveContext`.
- Event-driven strategy execution: engines call `on_start`, `on_market_data`, `on_schedule`, `on_resolution`, `on_stop` from `agenttrader/core/base_strategy.py`.
- Mode-specific execution model: fill behavior is selected by `ExecutionMode` in `agenttrader/data/models.py` and implemented in `agenttrader/core/price_fill_model.py` or `agenttrader/core/fill_model.py`.

## Backtest Flow (CLI + MCP)
1. Input strategy file is validated in `agenttrader/cli/validate.py`.
2. Run metadata is persisted in `backtest_runs` via `agenttrader/db/schema.py` and `agenttrader/data/cache.py`.
3. `BacktestEngine` in `agenttrader/core/backtest_engine.py` prefers DuckDB index (`agenttrader/data/index_adapter.py`) and falls back when needed.
4. Strategy receives market events through `StreamingBacktestContext` or `BacktestContext` in `agenttrader/core/context.py`.
5. Metrics are computed in `agenttrader/core/backtest_engine.py` and large outputs are written by `agenttrader/data/backtest_artifacts.py`.
6. MCP path in `agenttrader/mcp/server.py` wraps the same engine and stores progress snapshots in `backtest_runs.results_json`.

## Paper Trading Flow
1. Start request creates portfolio row (`paper_portfolios`) through `agenttrader/cli/paper.py` or `agenttrader/mcp/server.py`.
2. Detached daemon process is spawned by `agenttrader/core/paper_daemon.py:start_as_daemon`.
3. Daemon builds a `LiveContext` from `agenttrader/core/context.py` using `DataCache`, `OrderBookStore`, and `PmxtClient`.
4. Live polling and orderbook retrieval come from `agenttrader/data/pmxt_client.py`.
5. Orders update `trades`, `positions`, and cash balance via SQL writes in `agenttrader/core/context.py`.
6. Runtime heartbeat/status is written to `RUNTIME_DIR` JSON files via `agenttrader/core/paper_daemon.py`.

## Data Ingestion and Normalization Flow
1. Historical dataset download/extract is handled by `agenttrader/cli/dataset.py`.
2. Normalized DuckDB index is built in `agenttrader/data/index_builder.py` into `BACKTEST_INDEX_PATH`.
3. Live sync pulls PMXT candles/orderbooks in `agenttrader/cli/sync.py` or MCP `sync_data` in `agenttrader/mcp/server.py`.
4. Candles are normalized and repaired before cache persistence in `agenttrader/mcp/server.py` helper functions.
5. Orderbooks are compressed by day and written to filesystem via `agenttrader/data/orderbook_store.py`.

## State and Configuration Boundaries
- Filesystem/data roots are centrally resolved in `agenttrader/config.py`.
- DB schema lifecycle is migration-driven from `agenttrader/cli/config.py` using packaged Alembic config in `agenttrader/db/alembic.ini` and migrations in `agenttrader/db/migrations/versions`.
- Runtime schema checks are enforced in `agenttrader/db/health.py` before server startup in `agenttrader/mcp/server.py`.

## Cross-Cutting Concerns
- Structured error model is centralized in `agenttrader/errors.py` and surfaced uniformly by CLI wrappers in `agenttrader/cli/utils.py` and MCP payload builders in `agenttrader/mcp/server.py`.
- Performance telemetry is emitted to JSONL by `agenttrader/perf_logging.py` from both CLI and MCP call sites.
- PMXT sidecar conflict detection and safety guards are enforced in `agenttrader/mcp/server.py` before guarded operations.

