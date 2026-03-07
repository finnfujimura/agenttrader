# Tech Stack Map

## Snapshot
- Language/runtime: Python package targeting `>=3.12` (`pyproject.toml`).
- Delivery modes: CLI (`agenttrader`), MCP stdio server, and local HTTP dashboard.
- Primary domain: prediction-market research, backtesting, and paper trading across Polymarket/Kalshi.

## Runtime And Packaging
| Area | Stack | Evidence |
|---|---|---|
| Language | Python 3.12+ | `pyproject.toml` |
| Packaging | `setuptools` + `wheel` | `pyproject.toml` |
| CLI entrypoint | `click` group + subcommands | `agenttrader/cli/main.py` |
| Distributable assets | Dashboard static assets + Alembic migration files in package data | `MANIFEST.in`, `pyproject.toml` |

## Application Layers
| Layer | Key Modules | Role |
|---|---|---|
| CLI | `agenttrader/cli/main.py`, `agenttrader/cli/*.py` | User/agent command surface (`backtest`, `paper`, `sync`, `dataset`, `dashboard`, `mcp`, etc.) |
| MCP server | `agenttrader/mcp/server.py` | Tool API for MCP clients over stdio |
| Strategy/runtime core | `agenttrader/core/backtest_engine.py`, `agenttrader/core/context.py`, `agenttrader/core/paper_daemon.py` | Backtest execution, live paper loop, strategy callbacks |
| Data access/adapters | `agenttrader/data/source_selector.py`, `agenttrader/data/pmxt_client.py`, `agenttrader/data/parquet_adapter.py`, `agenttrader/data/index_adapter.py` | Unified market/history reads from live + historical sources |
| Persistence | `agenttrader/db/schema.py`, `agenttrader/data/cache.py`, `alembic/versions/*.py` | Operational DB models and migrations |
| Web dashboard | `agenttrader/dashboard/server.py`, `agenttrader/dashboard/static/index.html`, `agenttrader/dashboard/static/app.js` | Local API + SPA monitoring UI |

## Core Libraries By Concern
| Concern | Libraries | Evidence |
|---|---|---|
| CLI UX | `click`, `rich` | `agenttrader/cli/*.py`, `pyproject.toml` |
| MCP protocol | `mcp` | `agenttrader/mcp/server.py`, `pyproject.toml` |
| Local web API | `fastapi`, `uvicorn` | `agenttrader/dashboard/server.py`, `agenttrader/cli/dashboard.py`, `pyproject.toml` |
| SQL/ORM | `sqlalchemy`, `alembic` | `agenttrader/db/__init__.py`, `agenttrader/cli/config.py`, `alembic/env.py` |
| Historical analytics | `duckdb`, `numpy` | `agenttrader/data/index_builder.py`, `agenttrader/data/index_adapter.py`, `agenttrader/core/backtest_engine.py` |
| Live market connectivity | `pmxt`, retry via `tenacity` | `agenttrader/data/pmxt_client.py`, `pyproject.toml` |
| File serialization | `msgpack` + `gzip` | `agenttrader/data/orderbook_store.py`, `agenttrader/data/backtest_artifacts.py` |
| Config | `pyyaml` | `agenttrader/config.py` |
| File watching/hot reload | `watchdog` | `agenttrader/core/paper_daemon.py` |
| Dataset extraction | `zstandard` fallback path | `agenttrader/cli/dataset.py` |

## Data Stack
- Operational store: SQLite DB with WAL mode (`agenttrader/db/__init__.py`), schema in `agenttrader/db/schema.py`.
- Historical fast path: normalized DuckDB index at configured path (`agenttrader/config.py`, `agenttrader/data/index_adapter.py`).
- Historical raw fallback: parquet dataset adapter backed by DuckDB views (`agenttrader/data/parquet_adapter.py`).
- Source priority is explicit: normalized index > raw parquet > SQLite cache (`agenttrader/data/source_selector.py`).
- Extra persisted artifacts:
  - Orderbooks in `*.msgpack.gz` (`agenttrader/data/orderbook_store.py`).
  - Backtest artifact payloads in `*.msgpack.gz` (`agenttrader/data/backtest_artifacts.py`).

## Testing And Quality Tooling
- Test framework: `pytest`, async tests via `pytest-asyncio`, coverage via `pytest-cov` (`pyproject.toml`, `tests/unit/*.py`, `tests/integration/*.py`).
- Static analysis/formatting deps: `ruff`, `mypy` (`pyproject.toml`).

## Practical Notes For Planning
- This is a Python-first monorepo with no JS build toolchain; dashboard UI is plain static HTML/JS served by FastAPI (`agenttrader/dashboard/static/*`).
- Backtest and live paths share core strategy interfaces but operate on different data sources (`agenttrader/core/backtest_engine.py`, `agenttrader/core/paper_daemon.py`).
