# External Integrations Map

## Integration Inventory
| Integration | Type | Direction | Where Wired |
|---|---|---|---|
| PMXT SDK + sidecar | External SDK/service bridge | Outbound (market data fetch) | `agenttrader/data/pmxt_client.py`, `agenttrader/cli/sync.py`, `agenttrader/core/paper_daemon.py`, `agenttrader/mcp/server.py` |
| Polymarket + Kalshi (via PMXT) | External market platforms | Outbound via PMXT abstraction | `agenttrader/data/pmxt_client.py` |
| Jon Becker historical dataset | External dataset download | Outbound HTTP download + local ingest | `agenttrader/cli/dataset.py`, `agenttrader/data/index_builder.py`, `README.md` |
| MCP host clients (Claude/Cursor/Codex/etc.) | Protocol integration | Inbound stdio RPC | `agenttrader/cli/main.py`, `agenttrader/mcp/server.py`, `COMMANDS.md` |
| Local dashboard consumers | Local HTTP API integration | Inbound HTTP on localhost | `agenttrader/cli/dashboard.py`, `agenttrader/dashboard/server.py`, `agenttrader/dashboard/static/app.js` |

## PMXT Integration Details
- Client wrapper initializes both PMXT backends (`Polymarket`, `Kalshi`) in one adapter: `agenttrader/data/pmxt_client.py`.
- Live capabilities used:
  - Market discovery/search (`get_markets`, `search_markets`).
  - Live order book snapshots and midpoint pricing.
  - OHLCV/candlestick history pulls for sync.
  - Cross-platform market matching helpers.
- Reliability behavior:
  - Exponential retry wrapper (`tenacity`) around network-facing calls in `agenttrader/data/pmxt_client.py`.
  - Sidecar conflict guard (detect duplicate PMXT node sidecars) in `agenttrader/mcp/server.py`.
- Runtime dependency note:
  - PMXT path explicitly states Node.js sidecar requirement in `agenttrader/data/pmxt_client.py`.
  - Install guidance also references `pmxtjs` in `README.md`.

## Historical Data Integration
- Source archive is downloaded from S3 URL in `agenttrader/cli/dataset.py` (`DOWNLOAD_URL`).
- Download/extract paths:
  - Uses `aria2c` when present, otherwise Python downloader.
  - Extracts `.tar.zst` using system `tar+unzstd` or Python `zstandard` fallback.
- Ingestion pipeline:
  - Raw parquet folders under configured shared root (`agenttrader/config.py`, `agenttrader/cli/dataset.py`).
  - Normalization job writes DuckDB index (`agenttrader/data/index_builder.py`).
  - Runtime readers choose data source by priority (`agenttrader/data/source_selector.py`).

## MCP Protocol Surface
- `agenttrader mcp` starts stdio transport server (`agenttrader/cli/main.py`, `agenttrader/mcp/server.py`).
- Tool contract and argument surface are documented in `COMMANDS.md`.
- Server includes schema/init and data-source diagnostics (`debug_data_sources`) in `agenttrader/mcp/server.py`.

## Persistence And File-System Boundaries
- SQLite operational DB (market cache, paper state, backtest runs): `agenttrader/db/schema.py`, `agenttrader/data/cache.py`.
- Alembic-managed schema migrations executed during init: `agenttrader/cli/config.py`, `alembic/versions/*.py`.
- File-backed integrations:
  - Orderbook snapshots as `msgpack.gz`: `agenttrader/data/orderbook_store.py`.
  - Backtest artifacts as `msgpack.gz`: `agenttrader/data/backtest_artifacts.py`.
  - Runtime status/log files: `agenttrader/core/paper_daemon.py`, `agenttrader/config.py`.

## Config And Environment Touchpoints
- Path/environment overrides:
  - `AGENTTRADER_STATE_DIR`
  - `AGENTTRADER_DATA_ROOT`
  - Resolved in `agenttrader/config.py`.
- Sensitive keys are masked on `config set` output in `agenttrader/cli/config.py`.

## Planning Risks Around Integrations
- PMXT sidecar duplication can break auth/port pairing; guarded in `agenttrader/mcp/server.py`.
- Dataset/index availability materially changes backtest behavior and performance (`agenttrader/data/source_selector.py`, `agenttrader/data/index_adapter.py`).
- Live sync/data quality issues are surfaced as warnings/errors in `agenttrader/cli/sync.py` and `agenttrader/mcp/server.py`.
