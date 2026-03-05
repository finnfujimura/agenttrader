# Integrations

## External APIs and Market Data Providers
- Primary live-market integration: PMXT SDK via `agenttrader/data/pmxt_client.py`.
- PMXT clients instantiated as:
  - `pmxt.Polymarket()`
  - `pmxt.Kalshi()`
  (`agenttrader/data/pmxt_client.py`).
- Live integration capabilities used by this codebase:
  - Market discovery/search (`get_markets`, `search_markets`).
  - OHLCV/candles (`get_candlesticks_with_status` path).
  - Live orderbook snapshots (`get_live_snapshot`, `get_orderbook_snapshots_with_status`).
  - Cross-platform matching helpers (`get_matching_markets`).
- PMXT is treated as the only direct network market API abstraction inside app code (`agenttrader/data/pmxt_client.py`).
- Architectural rule repeated across modules: avoid direct PMXT imports outside wrapper (`# DO NOT import pmxt here...` header in many `agenttrader/*.py` files).

## PMXT Sidecar and Runtime Coupling
- PMXT requires Node-based sidecar; code explicitly warns about this in `agenttrader/data/pmxt_client.py`.
- MCP server guards against duplicate PMXT sidecars to prevent token/port mismatch (`agenttrader/mcp/server.py` with `PMXT_SIDECAR_PATH_FRAGMENT` and conflict detection helpers).
- Guarded MCP tools that rely on PMXT health: `match_markets`, `start_paper_trade`, `sync_data` (`agenttrader/mcp/server.py`).
- README operational prerequisite includes `npm install -g pmxtjs` (`README.md`).

## Data Backends and Persistence Integrations
- SQLite integration:
  - Engine/session setup in `agenttrader/db/__init__.py`.
  - Table models in `agenttrader/db/schema.py`.
  - Migration application through Alembic in `agenttrader/cli/config.py`.
- DuckDB integration:
  - Read-only normalized index access in `agenttrader/data/index_adapter.py`.
  - Index construction from parquet in `agenttrader/data/index_builder.py`.
- Parquet dataset integration:
  - Adapter reads local parquet into DuckDB views in `agenttrader/data/parquet_adapter.py`.
  - Dataset download/extract/verify commands in `agenttrader/cli/dataset.py`.
- Source routing integration:
  - Runtime priority chain `normalized-index -> raw-parquet -> sqlite-cache` in `agenttrader/data/source_selector.py`.

## External Dataset and Download Endpoints
- Historical dataset download endpoint hardcoded as `https://s3.jbecker.dev/data.tar.zst` in `agenttrader/cli/dataset.py`.
- Download path uses:
  - `aria2c` if available.
  - Python `urllib.request` fallback.
  - Extraction via system `tar --use-compress-program=unzstd` or Python `zstandard` fallback.
- Dataset is treated as external input for backtesting and index build (`agenttrader/cli/dataset.py`, `agenttrader/data/index_builder.py`).

## Protocol and Server Integrations
- MCP integration:
  - Server object and tool registry in `agenttrader/mcp/server.py`.
  - Stdio transport (`mcp.server.stdio`) in `agenttrader/mcp/server.py`.
  - CLI bridge command `agenttrader mcp` in `agenttrader/cli/main.py`.
- Local dashboard API integration:
  - FastAPI endpoints in `agenttrader/dashboard/server.py`.
  - Served by Uvicorn from `agenttrader/cli/dashboard.py`.
  - Static dashboard assets in `agenttrader/dashboard/static/`.

## Process and OS-Level Integrations
- Paper trading daemon spawns detached subprocesses (`agenttrader/core/paper_daemon.py` and `agenttrader/core/paper_daemon_runner.py`).
- Strategy hot-reload integrates filesystem monitoring via watchdog observer (`agenttrader/core/paper_daemon.py`).
- Runtime status written to JSON files under runtime dir (`agenttrader/core/paper_daemon.py` helpers `runtime_status_path` / `read_runtime_status`).

## Auth Providers and Secrets
- No first-party auth provider (no OAuth/OIDC/JWT/session framework integration) is implemented in code under `agenttrader/`.
- No inbound user auth middleware is configured for FastAPI dashboard routes in `agenttrader/dashboard/server.py`.
- Secret handling is minimal:
  - Config display redacts key names like `token`, `password`, `api_key` in `agenttrader/cli/config.py`.
  - PMXT credential/auth logic is delegated to the external PMXT SDK and sidecar (`agenttrader/data/pmxt_client.py`, `PMXT_API_REFERENCE.md`).

## Webhooks and Callback Surfaces
- No webhook receiver endpoints are defined (no `/webhook`-style routes in `agenttrader/dashboard/server.py` or `agenttrader/mcp/server.py`).
- Integration model is pull/poll based:
  - PMXT polling for live market snapshots in `agenttrader/core/context.py`.
  - Scheduled sync operations in `agenttrader/cli/sync.py` and MCP `sync_data` in `agenttrader/mcp/server.py`.

## Integration Constraints for User Strategies
- Strategy code is intentionally prevented from direct external API usage.
- Validator blocks imports like `requests`, `httpx`, `aiohttp`, `urllib`, and `pmxt` in `agenttrader/cli/validate.py`.
- Strategy interaction surface is restricted to `BaseStrategy` methods in `agenttrader/core/base_strategy.py`.

## Practical Summary
- Live external connectivity is concentrated in PMXT integration (`agenttrader/data/pmxt_client.py`).
- Historical backtesting integration is file/data-lake oriented (S3 dataset -> parquet -> DuckDB index).
- Operational tool integration surfaces are MCP stdio and local FastAPI dashboard; neither includes built-in user auth.
