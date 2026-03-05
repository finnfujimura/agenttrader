# Tech Stack

## Languages and Runtime
- Primary language: Python, required as `>=3.12` in `pyproject.toml`.
- Frontend/dashboard scripting: vanilla JavaScript in `agenttrader/dashboard/static/app.js`.
- Static markup/styling: HTML/CSS in `agenttrader/dashboard/static/index.html`.
- Python package build backend: setuptools (`setuptools.build_meta`) in `pyproject.toml`.
- Runtime split:
  - Python runtime for CLI/MCP/backtest/paper engine (`agenttrader/cli/main.py`, `agenttrader/mcp/server.py`).
  - Node runtime indirectly required by PMXT sidecar (see `agenttrader/data/pmxt_client.py` runtime error text and `README.md` install notes).

## Application Frameworks and Core Libraries
- CLI framework: Click (`agenttrader/cli/main.py` and subcommands under `agenttrader/cli/`).
- MCP framework: `mcp` SDK over stdio transport (`agenttrader/mcp/server.py`).
- HTTP server framework: FastAPI (`agenttrader/dashboard/server.py`).
- ASGI server: Uvicorn startup in `agenttrader/cli/dashboard.py`.
- ORM/data layer: SQLAlchemy models/sessions in `agenttrader/db/schema.py` and `agenttrader/db/__init__.py`.
- DB migrations: Alembic migration flow in `agenttrader/cli/config.py`, migration files in `alembic/versions/`.
- Data/analytics engine: DuckDB in `agenttrader/data/index_adapter.py`, `agenttrader/data/parquet_adapter.py`, `agenttrader/data/index_builder.py`.
- Retry/resilience: Tenacity decorators in `agenttrader/data/pmxt_client.py`.
- File watching for hot reload: watchdog observer in `agenttrader/core/paper_daemon.py`.

## Declared Dependencies (from `pyproject.toml`)
- Runtime deps: `pmxt`, `duckdb`, `click`, `zstandard`, `mcp`, `sqlalchemy`, `alembic`, `pyyaml`, `watchdog`, `msgpack`, `tenacity`, `fastapi`, `uvicorn`, `websockets`, `rich`, `numpy`, `pytz`.
- Dev deps: `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`, `mypy`, `pytz`.
- Notes from code usage:
  - `websockets` is declared but no direct import found under `agenttrader/` via repository grep.
  - `numpy`/`rich` are declared; core trading/data paths are mostly standard library + SQLAlchemy + DuckDB + PMXT.

## Data and Storage Stack
- Primary operational DB: SQLite, created with SQLAlchemy engine `sqlite:///...` in `agenttrader/db/__init__.py`.
- Schema managed by SQLAlchemy declarative models in `agenttrader/db/schema.py`.
- Schema evolution managed by Alembic revisions in `alembic/versions/0001_initial.py` through `0004_drop_legacy_price_history_constraint.py`.
- Historical backtest index: DuckDB file at configured `BACKTEST_INDEX_PATH` (`agenttrader/config.py`, `agenttrader/data/index_adapter.py`).
- Raw historical dataset: parquet files under configured shared data dir (`agenttrader/data/parquet_adapter.py`, `agenttrader/cli/dataset.py`).
- Orderbook snapshots: gzip+msgpack files in `ORDERBOOK_DIR` handled by `agenttrader/data/orderbook_store.py`.
- Backtest artifacts: gzip+msgpack files in `ARTIFACTS_DIR` via `agenttrader/data/backtest_artifacts.py`.

## Process and Execution Model
- Main CLI entrypoint: `agenttrader` console script from `pyproject.toml` pointing to `agenttrader.cli.main:cli`.
- MCP server launched as `agenttrader mcp` with stdio transport (`agenttrader/cli/main.py`, `agenttrader/mcp/server.py`).
- Paper trading uses a detached subprocess daemon (`agenttrader/core/paper_daemon.py`, `agenttrader/core/paper_daemon_runner.py`).
- Paper daemon loop uses asyncio with threaded file watcher trigger for strategy hot reload (`agenttrader/core/paper_daemon.py`).
- Backtesting supports multiple fidelity/execution modes configured through MCP/CLI tooling (`agenttrader/mcp/server.py`, `COMMANDS.md`).

## Configuration Surfaces
- Global/project path resolution and defaults in `agenttrader/config.py`.
- Environment variable overrides:
  - `AGENTTRADER_STATE_DIR`
  - `AGENTTRADER_DATA_ROOT`
  (resolved in `agenttrader/config.py`).
- Project-local path override file: `.agenttrader-paths.json` (read/write logic in `agenttrader/config.py` and init flow in `agenttrader/cli/config.py`).
- User config file: `config.yaml` generated/validated by `agenttrader/config.py`.
- Alembic runtime config:
  - Repo-level `alembic.ini` + `alembic/env.py`.
  - Packaged migration config `agenttrader/db/alembic.ini`.

## Packaging and Delivery
- Python package metadata and dependency definitions are centralized in `pyproject.toml`.
- Published package includes dashboard static assets and DB migration files via `[tool.setuptools.package-data]` in `pyproject.toml`.
- GitHub Actions publish workflow in `.github/workflows/publish.yml`.
- Versioning caveat: `pyproject.toml` version (`0.4.1`) differs from `agenttrader/__init__.py` (`0.1.1`), which is relevant for runtime version reporting.
