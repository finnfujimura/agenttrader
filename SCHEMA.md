# Database Schema Reference

agenttrader uses three data stores, each optimized for a different role.

```
~/.agenttrader/
├── db.sqlite                 # Market metadata, paper trading state, backtest records
├── backtest_index.duckdb     # Normalized historical trades (built from parquet)
└── data/                     # Raw parquet dataset (Polymarket + Kalshi)
```

---

## SQLite — `db.sqlite`

The primary operational database. Stores synced market data, paper trading state, and backtest records.

### `markets`

Prediction market metadata. Populated by `sync_data` or cache lookups.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | TEXT | PK | Market identifier (token ID for Polymarket, ticker for Kalshi) |
| `condition_id` | TEXT | | Platform condition/event ID |
| `platform` | TEXT | NOT NULL | `polymarket` or `kalshi` |
| `title` | TEXT | NOT NULL | Market question text |
| `category` | TEXT | | Category (`politics`, `crypto`, `sports`, etc.) |
| `tags` | TEXT | | JSON array of tags |
| `market_type` | TEXT | NOT NULL | `binary`, `categorical`, or `scalar` |
| `scalar_low` | FLOAT | | Lower bound (scalar markets only) |
| `scalar_high` | FLOAT | | Upper bound (scalar markets only) |
| `volume` | FLOAT | | Trading volume |
| `close_time` | INTEGER | | Unix timestamp of market close |
| `resolved` | INTEGER | DEFAULT 0 | `0` = active, `1` = resolved |
| `resolution` | TEXT | | Resolution outcome (`yes`, `no`, etc.) |
| `last_synced` | INTEGER | | Unix timestamp of last sync |

### `price_history`

Historical price data from candles/OHLCV syncs.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INTEGER | PK AUTOINCREMENT | |
| `market_id` | TEXT | NOT NULL | References `markets.id` |
| `platform` | TEXT | NOT NULL | Platform name |
| `timestamp` | INTEGER | NOT NULL | Unix timestamp |
| `yes_price` | FLOAT | NOT NULL | YES probability [0.0 - 1.0] |
| `no_price` | FLOAT | | NO probability [0.0 - 1.0] |
| `volume` | FLOAT | | Volume in this period |
| `source` | TEXT | DEFAULT `"pmxt"` | Data source (`pmxt`, `parquet`) |
| `granularity` | TEXT | DEFAULT `"1h"` | Candle interval (`1m`, `1h`, `1d`) |

**Unique:** `(market_id, timestamp)`, `(market_id, platform, timestamp)`

### `backtest_runs`

Backtest execution records.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | TEXT | PK | UUID |
| `strategy_path` | TEXT | NOT NULL | Path to strategy file |
| `strategy_hash` | TEXT | NOT NULL | SHA256 of strategy source |
| `start_date` | TEXT | NOT NULL | `YYYY-MM-DD` |
| `end_date` | TEXT | NOT NULL | `YYYY-MM-DD` |
| `initial_cash` | FLOAT | NOT NULL | Starting capital |
| `status` | TEXT | NOT NULL | `running`, `complete`, or `failed` |
| `error` | TEXT | | Traceback if failed |
| `results_json` | TEXT | | Full results payload (JSON) |
| `created_at` | INTEGER | NOT NULL | Creation timestamp |
| `completed_at` | INTEGER | | Completion timestamp |
| `execution_mode` | TEXT | DEFAULT `"strict_price_only"` | Execution mode used |

### `paper_portfolios`

Paper trading portfolio state.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | TEXT | PK | UUID |
| `strategy_path` | TEXT | NOT NULL | Path to strategy file |
| `strategy_hash` | TEXT | NOT NULL | SHA256 of strategy source |
| `initial_cash` | FLOAT | NOT NULL | Starting capital |
| `cash_balance` | FLOAT | NOT NULL | Current cash |
| `status` | TEXT | NOT NULL | `running` or `stopped` |
| `pid` | INTEGER | | OS process ID of daemon |
| `started_at` | INTEGER | NOT NULL | Start timestamp |
| `stopped_at` | INTEGER | | Stop timestamp |
| `last_reload` | INTEGER | | Last strategy hot-reload time |
| `reload_count` | INTEGER | DEFAULT 0 | Number of hot-reloads |

### `positions`

Open and closed positions within paper portfolios.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | TEXT | PK | UUID |
| `portfolio_id` | TEXT | NOT NULL | References `paper_portfolios.id` |
| `market_id` | TEXT | NOT NULL | Market identifier |
| `platform` | TEXT | NOT NULL | Platform name |
| `side` | TEXT | NOT NULL | `yes` or `no` |
| `contracts` | FLOAT | NOT NULL | Quantity held |
| `avg_cost` | FLOAT | NOT NULL | Average entry price |
| `opened_at` | INTEGER | NOT NULL | Open timestamp |
| `closed_at` | INTEGER | | Close timestamp |
| `realized_pnl` | FLOAT | | P&L when closed |

### `trades`

Individual trade execution records.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | TEXT | PK | UUID |
| `portfolio_id` | TEXT | NOT NULL | References `paper_portfolios.id` |
| `market_id` | TEXT | NOT NULL | Market identifier |
| `platform` | TEXT | NOT NULL | Platform name |
| `action` | TEXT | NOT NULL | `buy` or `sell` |
| `side` | TEXT | NOT NULL | `yes` or `no` |
| `contracts` | FLOAT | NOT NULL | Quantity |
| `price` | FLOAT | NOT NULL | Execution price |
| `slippage` | FLOAT | NOT NULL, DEFAULT 0 | Slippage amount |
| `filled_at` | INTEGER | NOT NULL | Execution timestamp |
| `pnl` | FLOAT | | Realized P&L |

### `strategy_logs`

Strategy runtime log messages.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INTEGER | PK AUTOINCREMENT | |
| `portfolio_id` | TEXT | NOT NULL | References `paper_portfolios.id` |
| `timestamp` | INTEGER | NOT NULL | Log timestamp |
| `message` | TEXT | NOT NULL | Log message |

---

## DuckDB — `backtest_index.duckdb`

Normalized historical trade index built from the raw parquet dataset. Optimized for fast time-range queries over thousands of markets.

**Built by:** `agenttrader dataset build-index`
**Size:** ~13 GB (depends on dataset)

### `normalized_trades`

Every trade across Polymarket and Kalshi, normalized to a common schema.

| Column | Type | Description |
|--------|------|-------------|
| `market_id` | TEXT | Market identifier (token ID or ticker) |
| `platform` | TEXT | `polymarket` or `kalshi` |
| `ts` | BIGINT | Unix timestamp of trade |
| `yes_price` | DOUBLE | YES probability [0.0 - 1.0] |
| `volume` | DOUBLE | Trade volume |

**Index:** `idx_trades_market_ts(market_id, ts)`

### `market_metadata`

Aggregated statistics per market, derived from `normalized_trades`.

| Column | Type | Description |
|--------|------|-------------|
| `market_id` | TEXT | Market identifier |
| `platform` | TEXT | `polymarket` or `kalshi` |
| `min_ts` | BIGINT | Earliest trade timestamp |
| `max_ts` | BIGINT | Latest trade timestamp |
| `n_trades` | BIGINT | Total number of trades |
| `avg_price` | DOUBLE | Average YES price |

**Index:** `idx_meta_platform(platform)`

### How Trades Are Normalized

**Polymarket:** Raw trades are CLOB fills between maker/taker with token amounts. The build process joins trades with block timestamps and market token maps, then converts token ratios to YES probabilities. Both sides of each trade (YES taker, YES maker) are captured. Prices outside [0.001, 0.999] are filtered out.

**Kalshi:** Raw trades have a direct `yes_price` field (0-100 scale). The build process divides by 100 to normalize to [0.0, 1.0]. Prices outside [0.01, 0.99] are filtered out.

---

## Parquet Dataset

Raw historical data from the Jon Becker prediction market dataset. This is the source of truth that gets normalized into the DuckDB index.

**Location:** `~/.agenttrader/data/`
**Size:** ~36 GB
**Downloaded by:** `agenttrader dataset download`

### Directory Structure

```
data/
├── polymarket/
│   ├── markets/*.parquet     # Market metadata (id, condition_id, clob_token_ids, question, volume, ...)
│   ├── trades/*.parquet      # CLOB trade fills (block_number, maker/taker amounts and asset IDs)
│   └── blocks/*.parquet      # Block number -> timestamp mapping
└── kalshi/
    ├── markets/*.parquet     # Market metadata (ticker, event_ticker, title, status, ...)
    └── trades/*.parquet      # Trades (ticker, yes_price, count, created_time)
```

### Key Parquet Columns

**Polymarket markets:** `id`, `condition_id`, `clob_token_ids` (JSON array), `question`, `slug`, `volume`, `closed`, `end_date`, `outcome_prices`

**Polymarket trades:** `block_number`, `transaction_hash`, `maker_asset_id`, `taker_asset_id`, `maker_amount`, `taker_amount`

**Polymarket blocks:** `block_number`, `timestamp`

**Kalshi markets:** `ticker`, `event_ticker`, `title`, `category`, `status`, `market_type`, `volume`, `close_time`, `result`

**Kalshi trades:** `ticker`, `yes_price` (0-100), `count`, `created_time`

---

## Data Source Priority

Tools select the best available source automatically:

| Priority | Source | Backing Store | Best For |
|----------|--------|---------------|----------|
| 1 | Normalized Index | DuckDB | Backtesting (fastest) |
| 2 | Raw Parquet | DuckDB (in-memory views) | Backtesting fallback |
| 3 | SQLite Cache | SQLite | Paper trading, live data |

If the DuckDB index is built, it is always preferred. The SQLite cache is always available as a fallback but requires `sync_data` to populate.
