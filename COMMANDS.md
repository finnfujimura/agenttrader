# MCP Tool Reference

Complete reference for all agenttrader MCP tools. These are the tools available when running agenttrader as an MCP server.

---

## Data Query Tools

### `get_markets`

List prediction markets. Uses the best available data source (index > parquet > cache).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `platform` | string | `"all"` | `"polymarket"`, `"kalshi"`, or `"all"` |
| `category` | string | | Filter by category (e.g. `"politics"`, `"crypto"`) |
| `tags` | string[] | | Filter by tags |
| `market_ids` | string[] | | Look up specific markets by ID. Bypasses volume/limit ordering |
| `include_capabilities` | boolean | `false` | Include backtest/history/sync capability annotations per market |
| `limit` | integer | `20` | Max results |

**Example:**
```json
{
  "tool": "get_markets",
  "args": {
    "platform": "polymarket",
    "category": "politics",
    "limit": 10,
    "include_capabilities": true
  }
}
```

---

### `get_price`

Get latest price for a market.

| Parameter | Type | Default | Required | Description |
|-----------|------|---------|----------|-------------|
| `market_id` | string | | Yes | Market identifier |
| `platform` | string | `"polymarket"` | | `"polymarket"` or `"kalshi"` |

**Example:**
```json
{
  "tool": "get_price",
  "args": {
    "market_id": "KXFEDDECISION-25DEC-H0",
    "platform": "kalshi"
  }
}
```

---

### `get_history`

Get market history analytics. Raw history points are omitted by default.

| Parameter | Type | Default | Required | Description |
|-----------|------|---------|----------|-------------|
| `market_id` | string | | Yes | Market identifier |
| `days` | integer | `7` | | Lookback window in days |
| `platform` | string | `"polymarket"` | | Platform hint. Required for indexed/parquet sources |
| `include_raw` | boolean | `false` | | Include raw price point arrays |

**Returns:** Analytics object with `current_price`, `avg_7d_price`, `price_change_24h`, `trend_direction`, `volatility`, `points`, `last_point_timestamp`.

**Example:**
```json
{
  "tool": "get_history",
  "args": {
    "market_id": "will-bitcoin-hit-100k",
    "platform": "polymarket",
    "days": 30,
    "include_raw": true
  }
}
```

---

### `match_markets`

Match equivalent markets across Polymarket and Kalshi.

| Parameter | Type | Description |
|-----------|------|-------------|
| `polymarket_slug` | string | Polymarket market slug to find Kalshi equivalents |
| `kalshi_ticker` | string | Kalshi ticker to find Polymarket equivalents |

Provide one or the other. The tool searches the opposite platform for matching markets.

**Example:**
```json
{
  "tool": "match_markets",
  "args": {
    "polymarket_slug": "will-bitcoin-hit-100k"
  }
}
```

---

## Research Tools

### `research_markets`

Compound workflow: list filtered markets and return history analytics for each in a single call. Each market includes capability annotations (backtest availability, cache status, sync eligibility).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `days` | integer | `7` | History lookback window |
| `platform` | string | `"all"` | `"polymarket"`, `"kalshi"`, or `"all"` |
| `category` | string | | Filter by category |
| `tags` | string[] | | Filter by tags |
| `market_ids` | string[] | | Look up specific markets |
| `limit` | integer | `20` | Max markets |
| `sync_first` | boolean | `false` | Sync live data before researching (sqlite-cache only) |
| `sync_limit` | integer | `100` | Max markets to sync when `sync_first=true` |
| `include_raw` | boolean | `false` | Include raw price point arrays |
| `active_only` | boolean | `true` | Filter out resolved/expired markets |

**Example:**
```json
{
  "tool": "research_markets",
  "args": {
    "platform": "polymarket",
    "category": "politics",
    "days": 30,
    "limit": 5,
    "active_only": true
  }
}
```

---

## Backtest Tools

### `run_backtest`

Run a strategy backtest against historical data.

| Parameter | Type | Default | Required | Description |
|-----------|------|---------|----------|-------------|
| `strategy_path` | string | | Yes | Path to strategy Python file |
| `start_date` | string | | Yes | Start date (`YYYY-MM-DD`) |
| `end_date` | string | | Yes | End date (`YYYY-MM-DD`) |
| `initial_cash` | number | `10000` | | Starting capital |
| `max_markets` | integer | | | Cap number of markets (default: no limit) |
| `fidelity` | string | `"exact_trade"` | | `"exact_trade"` (every trade), `"bar_1h"` (hourly), `"bar_1d"` (daily) |
| `execution_mode` | string | `"strict_price_only"` | | See [Execution Modes](#execution-modes) |
| `include_curve` | boolean | `false` | | Include full equity curve and trades arrays |

**Example:**
```json
{
  "tool": "run_backtest",
  "args": {
    "strategy_path": "./strategies/mean_reversion.py",
    "start_date": "2024-01-01",
    "end_date": "2024-12-31",
    "initial_cash": 10000,
    "fidelity": "bar_1h",
    "max_markets": 50
  }
}
```

---

### `validate_and_backtest`

Compound workflow: validate a strategy file, then run the backtest if valid. Same parameters as `run_backtest`.

| Parameter | Type | Default | Required | Description |
|-----------|------|---------|----------|-------------|
| `strategy_path` | string | | Yes | Path to strategy Python file |
| `start_date` | string | | Yes | Start date (`YYYY-MM-DD`) |
| `end_date` | string | | Yes | End date (`YYYY-MM-DD`) |
| `initial_cash` | number | `10000` | | Starting capital |
| `max_markets` | integer | | | Cap number of markets |
| `fidelity` | string | `"exact_trade"` | | `"exact_trade"`, `"bar_1h"`, or `"bar_1d"` |
| `execution_mode` | string | `"strict_price_only"` | | See [Execution Modes](#execution-modes) |
| `include_curve` | boolean | `false` | | Include full equity curve and trades arrays |

**Example:**
```json
{
  "tool": "validate_and_backtest",
  "args": {
    "strategy_path": "./strategies/momentum.py",
    "start_date": "2024-06-01",
    "end_date": "2024-12-31"
  }
}
```

---

### `validate_strategy`

Validate a strategy file without running it. Checks for: exactly one `BaseStrategy` subclass, correct method signatures, no forbidden imports (network libraries), and only allowed API calls.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `strategy_path` | string | Yes | Path to strategy Python file |

---

### `get_backtest`

Retrieve results for a completed backtest run.

| Parameter | Type | Default | Required | Description |
|-----------|------|---------|----------|-------------|
| `run_id` | string | | Yes | Backtest run UUID |
| `include_curve` | boolean | `false` | | Include full equity curve and trades arrays |

---

### `list_backtests`

List all recent backtest runs. No parameters.

---

## Paper Trading Tools

### `start_paper_trade`

Start a paper trading daemon for a strategy.

| Parameter | Type | Default | Required | Description |
|-----------|------|---------|----------|-------------|
| `strategy_path` | string | | Yes | Path to strategy Python file |
| `initial_cash` | number | `10000` | | Starting capital |

---

### `get_portfolio`

Get current status of a paper trading portfolio (positions, cash, unrealized P&L).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `portfolio_id` | string | Yes | Portfolio UUID |

---

### `stop_paper_trade`

Stop a running paper trading daemon.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `portfolio_id` | string | Yes | Portfolio UUID |

---

### `list_paper_trades`

List all paper trading portfolios. No parameters.

---

## Data Management Tools

### `sync_data`

Sync live market data from PMXT into the local SQLite cache. Required for paper trading. Not needed for backtesting when the indexed dataset is available.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `days` | integer | `7` | Lookback period for candle data |
| `platform` | string | `"all"` | Platform filter |
| `market_ids` | string[] | | Sync specific markets by ID |
| `limit` | integer | `100` | Max markets to sync (when discovering) |
| `category` | string | | Filter by category |
| `resolved` | boolean | `false` | Include resolved/closed markets |
| `granularity` | string | `"hourly"` | `"minute"`, `"hourly"`, or `"daily"` |

**Example — broad sync:**
```json
{
  "tool": "sync_data",
  "args": {
    "platform": "polymarket",
    "days": 30,
    "limit": 20
  }
}
```

**Example — targeted sync by ID:**
```json
{
  "tool": "sync_data",
  "args": {
    "market_ids": ["will-bitcoin-hit-100k"],
    "platform": "polymarket",
    "days": 7
  }
}
```

---

### `debug_data_sources`

Diagnose data source availability. Returns status of DuckDB index, parquet files, SQLite cache, and schema health. Use this first when data lookups fail or return unexpected results. No parameters.

---

## Execution Modes

Backtests support three execution modes controlling how trades are filled:

| Mode | Default | Fill Behavior | Orderbook Access |
|------|---------|---------------|------------------|
| `strict_price_only` | Yes | Fills at observed price, zero slippage | `get_orderbook()` returns `None` |
| `observed_orderbook` | No | Uses real stored orderbook snapshots | Raises if no observed orderbook exists |
| `synthetic_execution_model` | No | Synthesizes orderbooks for approximate fill modeling | Always available (modeled) |

`strict_price_only` is the default because historical orderbook data is not available in the parquet dataset. Synthetic orderbooks can produce misleadingly optimistic results.

## Market Capabilities

When `include_capabilities` is set (always on for `research_markets`, opt-in for `get_markets`), each market includes a capabilities object:

```json
{
  "capabilities": {
    "backtest": {
      "index_available": true,
      "index_start": "2024-06-01",
      "index_end": "2025-02-15"
    },
    "history": {
      "cache_available": true,
      "last_point_timestamp": "2026-02-27T18:00:00+00:00"
    },
    "sync": {
      "can_attempt_live_sync": true
    }
  }
}
```

This tells an agent upfront whether a market can be backtested, has cached history, or is eligible for live sync — eliminating trial-and-error discovery.
