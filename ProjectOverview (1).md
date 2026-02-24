# agenttrader

> A Python CLI + MCP server for AI agents to research, write, backtest, and paper trade prediction market strategies on Polymarket and Kalshi.

---

## Vision

`agenttrader` is a developer toolkit built primarily for **agentic use**. An AI agent (running in Claude Code, OpenAI Codex, Cursor, etc.) should be able to — without any human intervention:

1. Discover live and historical prediction markets
2. Write a strategy as a local `.py` file
3. Backtest it against cached historical data
4. Iterate on the strategy based on structured results
5. Deploy it as a running paper trading daemon

The entire loop from zero to a live paper trading deployment should complete in a single agent session.

---

## Installation

```bash
pip install agenttrader
```

After install, two entry points are available:
- `agenttrader` — the CLI tool
- `agenttrader mcp` — starts the MCP server (stdio transport)

---

## Project File Structure

```
agenttrader/
├── agenttrader/
│   ├── __init__.py
│   ├── cli/
│   │   ├── __init__.py
│   │   ├── main.py              # Click group entry point
│   │   ├── markets.py           # `agenttrader markets` subcommands
│   │   ├── sync.py              # `agenttrader sync` subcommand
│   │   ├── backtest.py          # `agenttrader backtest` subcommands
│   │   ├── paper.py             # `agenttrader paper` subcommands
│   │   ├── validate.py          # `agenttrader validate` subcommand
│   │   ├── dashboard.py         # `agenttrader dashboard` subcommand
│   │   └── config.py            # `agenttrader config` subcommands
│   ├── mcp/
│   │   ├── __init__.py
│   │   └── server.py            # MCP stdio server, tool definitions
│   ├── core/
│   │   ├── __init__.py
│   │   ├── base_strategy.py     # BaseStrategy class users subclass
│   │   ├── context.py           # ExecutionContext (injected into strategies)
│   │   ├── backtest_engine.py   # BacktestEngine + BacktestContext
│   │   ├── paper_daemon.py      # PaperTrader background daemon
│   │   ├── fill_model.py        # Orderbook-based slippage/fill simulation
│   │   └── scheduler.py         # on_schedule() timer logic
│   ├── data/
│   │   ├── __init__.py
│   │   ├── dome_client.py       # All Dome API calls — only file that imports dome_api_sdk
│   │   ├── models.py            # Internal dataclasses: Market, Price, OrderBook, etc.
│   │   ├── cache.py             # SQLite read/write for market metadata + trade data
│   │   └── orderbook_store.py   # Compressed orderbook file storage (~/.agenttrader/orderbooks/)
│   ├── db/
│   │   ├── __init__.py
│   │   ├── schema.py            # SQLAlchemy models
│   │   └── migrations/          # Alembic migrations
│   └── dashboard/
│       ├── __init__.py
│       ├── server.py            # FastAPI server, localhost:8080
│       └── static/              # Compiled React frontend (read-only monitoring UI)
├── tests/
│   ├── unit/
│   │   ├── test_base_strategy.py
│   │   ├── test_backtest_engine.py
│   │   ├── test_fill_model.py
│   │   ├── test_dome_client.py
│   │   └── test_models.py
│   ├── integration/
│   │   ├── test_backtest_full.py
│   │   ├── test_paper_daemon.py
│   │   └── test_cli_commands.py
│   └── fixtures/
│       ├── sample_markets.json
│       ├── sample_orderbooks.json
│       └── sample_strategy.py
├── pyproject.toml
├── README.md
└── CHANGELOG.md
```

---

## Local Storage Structure

All state is stored under `~/.agenttrader/`. No cloud, no server, no auth required.

```
~/.agenttrader/
├── config.yaml              # Dome API key, preferences
├── db.sqlite                # All metadata, trade logs, backtest results, market cache
└── orderbooks/              # Compressed orderbook snapshot files
    ├── polymarket/
    │   └── <condition_id>/
    │       ├── 2024-01-01.msgpack.gz
    │       ├── 2024-01-02.msgpack.gz
    │       └── ...
    └── kalshi/
        └── <ticker>/
            ├── 2024-01-01.msgpack.gz
            └── ...
```

**Why split storage:**
- `db.sqlite` holds everything except raw orderbook snapshots (fast queries, small footprint)
- Orderbook files are compressed MessagePack — ~50KB per market per day at hourly granularity
- 100 markets × 90 days = ~450MB total, manageable and prunable

---

## Database Schema (`db.sqlite`)

```sql
-- Cached market metadata (refreshed hourly via sync)
CREATE TABLE markets (
    id TEXT PRIMARY KEY,                  -- platform-native ID (token_id or ticker)
    condition_id TEXT,                    -- Polymarket condition_id / Kalshi event ticker
    platform TEXT NOT NULL,              -- 'polymarket' | 'kalshi'
    title TEXT NOT NULL,
    category TEXT,
    tags TEXT,                           -- JSON array stored as text
    market_type TEXT NOT NULL,           -- 'binary' | 'categorical' | 'scalar'
    scalar_low REAL,                     -- NULL unless market_type = 'scalar'
    scalar_high REAL,                    -- NULL unless market_type = 'scalar'
    volume REAL,
    close_time INTEGER,                  -- Unix timestamp
    resolved INTEGER DEFAULT 0,         -- 0 or 1
    resolution TEXT,                     -- 'yes' | 'no' | scalar value | NULL
    last_synced INTEGER                  -- Unix timestamp
);

-- Price history (candlestick, hourly by default)
CREATE TABLE price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    timestamp INTEGER NOT NULL,          -- Unix timestamp
    yes_price REAL NOT NULL,             -- 0.0 to 1.0
    no_price REAL,
    volume REAL,
    UNIQUE(market_id, timestamp)
);

-- Backtest run metadata and results
CREATE TABLE backtest_runs (
    id TEXT PRIMARY KEY,                 -- UUID
    strategy_path TEXT NOT NULL,
    strategy_hash TEXT NOT NULL,         -- sha256 of strategy file at run time
    start_date TEXT NOT NULL,            -- ISO date string
    end_date TEXT NOT NULL,
    initial_cash REAL NOT NULL,
    status TEXT NOT NULL,                -- 'running' | 'complete' | 'failed'
    error TEXT,                          -- NULL unless status = 'failed'
    results_json TEXT,                   -- NULL until complete; see Results Schema below
    created_at INTEGER NOT NULL,
    completed_at INTEGER
);

-- Paper trading portfolios
CREATE TABLE paper_portfolios (
    id TEXT PRIMARY KEY,                 -- UUID
    strategy_path TEXT NOT NULL,
    strategy_hash TEXT NOT NULL,
    initial_cash REAL NOT NULL,
    cash_balance REAL NOT NULL,
    status TEXT NOT NULL,                -- 'running' | 'stopped'
    pid INTEGER,                         -- OS process ID of daemon
    started_at INTEGER NOT NULL,
    stopped_at INTEGER
);

-- Open and closed positions
CREATE TABLE positions (
    id TEXT PRIMARY KEY,                 -- UUID
    portfolio_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    side TEXT NOT NULL,                  -- 'yes' | 'no'
    contracts REAL NOT NULL,
    avg_cost REAL NOT NULL,
    opened_at INTEGER NOT NULL,
    closed_at INTEGER,                   -- NULL if still open
    realized_pnl REAL                    -- NULL if still open
);

-- Immutable trade ledger
CREATE TABLE trades (
    id TEXT PRIMARY KEY,                 -- UUID
    portfolio_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    action TEXT NOT NULL,                -- 'buy' | 'sell' | 'resolution'
    side TEXT NOT NULL,                  -- 'yes' | 'no'
    contracts REAL NOT NULL,
    price REAL NOT NULL,
    slippage REAL NOT NULL DEFAULT 0,
    filled_at INTEGER NOT NULL,
    pnl REAL                             -- NULL unless action = 'sell' or 'resolution'
);
```

---

## Internal Data Models (`data/models.py`)

These are the canonical internal types. **Nothing outside `data/dome_client.py` ever imports from `dome_api_sdk` directly.** All Dome API responses are translated into these models at the boundary.

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class Platform(str, Enum):
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"

class MarketType(str, Enum):
    BINARY      = "binary"       # YES/NO, settles at $1 or $0
    CATEGORICAL = "categorical"  # Multiple named outcomes, one wins
    SCALAR      = "scalar"       # Numeric range (Kalshi-specific)

@dataclass
class Market:
    id: str                          # platform-native ID
    condition_id: str                # Polymarket condition_id or Kalshi event ticker
    platform: Platform
    title: str
    category: str
    tags: list[str]
    market_type: MarketType
    volume: float
    close_time: int                  # Unix timestamp
    resolved: bool
    resolution: Optional[str]        # 'yes' | 'no' | numeric string | None
    scalar_low: Optional[float]      # Only set when market_type == SCALAR
    scalar_high: Optional[float]     # Only set when market_type == SCALAR

@dataclass
class PricePoint:
    timestamp: int
    yes_price: float                 # 0.0 to 1.0
    no_price: Optional[float]
    volume: float

@dataclass
class OrderLevel:
    price: float
    size: float                      # number of contracts available at this price

@dataclass
class OrderBook:
    market_id: str
    timestamp: int
    bids: list[OrderLevel]           # sorted descending by price
    asks: list[OrderLevel]           # sorted ascending by price

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return None

@dataclass
class Position:
    id: str
    market_id: str
    platform: Platform
    side: str                        # 'yes' | 'no'
    contracts: float
    avg_cost: float
    opened_at: int

@dataclass
class FillResult:
    filled: bool
    fill_price: float                # actual average fill price after slippage
    contracts: float
    slippage: float                  # fill_price - mid_price
    partial: bool                    # True if order was only partially filled
```

---

## BaseStrategy Interface (`core/base_strategy.py`)

This is the **only** file users interact with when writing strategies. It must remain stable after v1.0. Do not add methods without versioning.

```python
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from agenttrader.core.context import ExecutionContext
    from agenttrader.data.models import Market, OrderBook, Position, PricePoint

class BaseStrategy(ABC):
    """
    Subclass this to create a trading strategy.

    Strategies are plain .py files. The execution engine (backtester or
    paper trader) imports your class and calls the lifecycle hooks below.

    RULES:
    - Do NOT import dome_api_sdk or make network calls directly.
    - Do NOT import requests, httpx, or any networking library.
    - All data access must go through self.* methods.
    - All order placement must go through self.buy() and self.sell().
    """

    def __init__(self, context: "ExecutionContext"):
        self._ctx = context

    # ------------------------------------------------------------------ #
    # Lifecycle hooks — override these in your strategy                   #
    # ------------------------------------------------------------------ #

    def on_start(self) -> None:
        """Called once when strategy initializes. Subscribe to markets here."""
        pass

    @abstractmethod
    def on_market_data(self, market: "Market", price: float, orderbook: "OrderBook") -> None:
        """
        Called on every price update for all subscribed markets.

        Args:
            market:    The market that updated. Check market.market_type
                       to know if it's BINARY, CATEGORICAL, or SCALAR.
            price:     Current YES price (0.0 to 1.0).
            orderbook: Current orderbook snapshot. Use for slippage estimates.
        """
        pass

    def on_resolution(self, market: "Market", outcome: str, pnl: float) -> None:
        """
        Called when a subscribed market resolves.

        Args:
            market:  The resolved market.
            outcome: 'yes' | 'no' for BINARY. Winning option name for
                     CATEGORICAL. Numeric string for SCALAR.
            pnl:     Realized P&L from this resolution in dollars.
        """
        pass

    def on_schedule(self, now: datetime, market: "Market") -> None:
        """
        Called every 15 minutes (configurable) for each subscribed market.
        Use for time-decay strategies and pre-expiry position management.

        Example:
            hours_left = (market.close_time - now.timestamp()) / 3600
            if hours_left < 2 and self.get_position(market.id):
                self.sell(market.id)
                self.log("Closing position 2h before market close")
        """
        pass

    def on_stop(self) -> None:
        """Called on graceful shutdown."""
        pass

    # ------------------------------------------------------------------ #
    # Market subscription                                                 #
    # ------------------------------------------------------------------ #

    def subscribe(
        self,
        platform: str = "all",           # 'polymarket' | 'kalshi' | 'all'
        category: Optional[str] = None,
        tags: Optional[list[str]] = None,
        market_ids: Optional[list[str]] = None,
    ) -> None:
        """Subscribe to market price updates. Call in on_start()."""
        self._ctx.subscribe(platform, category, tags, market_ids)

    def search_markets(self, query: str, platform: str = "all") -> list["Market"]:
        """Search cached markets by keyword. Returns list of Market objects."""
        return self._ctx.search_markets(query, platform)

    # ------------------------------------------------------------------ #
    # Data access                                                         #
    # ------------------------------------------------------------------ #

    def get_price(self, market_id: str) -> float:
        """Current YES price (0.0–1.0) from local cache."""
        return self._ctx.get_price(market_id)

    def get_orderbook(self, market_id: str) -> "OrderBook":
        """Most recent orderbook snapshot from local cache."""
        return self._ctx.get_orderbook(market_id)

    def get_history(self, market_id: str, lookback_hours: int = 24) -> list["PricePoint"]:
        """
        Historical price points from local cache.
        Only returns data for timestamps <= current execution time
        (safe for backtesting — no look-ahead bias).
        """
        return self._ctx.get_history(market_id, lookback_hours)

    # ------------------------------------------------------------------ #
    # Portfolio                                                           #
    # ------------------------------------------------------------------ #

    def get_position(self, market_id: str) -> Optional["Position"]:
        """Returns open position for this market, or None."""
        return self._ctx.get_position(market_id)

    def get_cash(self) -> float:
        """Current available cash balance."""
        return self._ctx.get_cash()

    def get_portfolio_value(self) -> float:
        """Cash + mark-to-market value of all open positions."""
        return self._ctx.get_portfolio_value()

    # ------------------------------------------------------------------ #
    # Order execution                                                     #
    # ------------------------------------------------------------------ #

    def buy(
        self,
        market_id: str,
        contracts: float,
        side: str = "yes",              # 'yes' | 'no'
        order_type: str = "market",     # 'market' | 'limit'
        limit_price: Optional[float] = None,
    ) -> str:
        """
        Place a buy order. Returns order_id string.
        Raises InsufficientCashError if cash < contracts * price.
        Raises MarketNotFoundError if market_id not in local cache.
        """
        return self._ctx.buy(market_id, contracts, side, order_type, limit_price)

    def sell(self, market_id: str, contracts: Optional[float] = None) -> str:
        """
        Sell position. contracts=None sells the entire position.
        Returns order_id string.
        Raises NoPositionError if no open position for this market.
        """
        return self._ctx.sell(market_id, contracts)

    # ------------------------------------------------------------------ #
    # Utilities                                                           #
    # ------------------------------------------------------------------ #

    def log(self, message: str) -> None:
        """Append to the strategy execution log (visible in dashboard)."""
        self._ctx.log(message)

    def set_state(self, key: str, value) -> None:
        """Persist arbitrary state across on_market_data() calls."""
        self._ctx.set_state(key, value)

    def get_state(self, key: str, default=None):
        """Retrieve previously set state."""
        return self._ctx.get_state(key, default)
```

---

## CLI Reference

Every command supports `--json` for structured machine-readable output. **All errors, including Python tracebacks from strategy code, are returned as structured JSON when `--json` is active.**

### Setup

```bash
agenttrader init
# Creates ~/.agenttrader/ directory structure and empty db.sqlite

agenttrader config set dome_api_key <YOUR_KEY>
agenttrader config set schedule_interval_minutes 15   # on_schedule() call frequency
agenttrader config get dome_api_key
agenttrader config show
```

### Markets

```bash
# List markets
agenttrader markets list
agenttrader markets list --platform polymarket
agenttrader markets list --platform kalshi --category politics --limit 20
agenttrader markets list --tags crypto --min-volume 50000
agenttrader markets list --json

# Get current price
agenttrader markets price <market_id>
agenttrader markets price <market_id> --json

# Get price history
agenttrader markets history <market_id>
agenttrader markets history <market_id> --days 30 --json

# Cross-platform matching
agenttrader markets match --polymarket-slug "nfl-chiefs-vs-eagles"
agenttrader markets match --kalshi-ticker "KXNFLGAME-25SEP07KCCPHIL"
```

**Example `--json` output for `markets list`:**
```json
{
  "ok": true,
  "count": 2,
  "markets": [
    {
      "id": "98250445447699368679516529207365255018790721464590833209064266254238063117329",
      "condition_id": "0x4567b275e6b667a6217f5cb4f06a797d3a1eaf1d0281fb5bc8c75e2046ae7e57",
      "platform": "polymarket",
      "title": "Will Biden withdraw before the 2024 election?",
      "category": "politics",
      "tags": ["politics", "usa"],
      "market_type": "binary",
      "scalar_low": null,
      "scalar_high": null,
      "volume": 4823910.50,
      "close_time": 1722470400,
      "resolved": true,
      "resolution": "yes"
    }
  ]
}
```

**Example `--json` error output:**
```json
{
  "ok": false,
  "error": "DomeAPIError",
  "message": "Rate limit exceeded. Retry after 2 seconds.",
  "retry_after": 2
}
```

### Sync (Data Cache)

```bash
# Sync top 100 markets by volume (default)
agenttrader sync

# Sync with specific options
agenttrader sync --days 90
agenttrader sync --days 90 --granularity hourly    # default
agenttrader sync --days 90 --granularity minute    # warning: large data
agenttrader sync --markets <id1> <id2> <id3>       # specific markets only
agenttrader sync --platform polymarket
agenttrader sync --platform kalshi --category politics
agenttrader sync --json

# Prune old cache data
agenttrader prune --older-than 90d
agenttrader prune --older-than 90d --dry-run       # shows what would be deleted
```

**Example `--json` output for `sync`:**
```json
{
  "ok": true,
  "markets_synced": 47,
  "price_points_fetched": 101520,
  "orderbook_files_written": 4230,
  "disk_used_mb": 214.3,
  "errors": []
}
```

### Validate

```bash
agenttrader validate ./strategy.py
agenttrader validate ./strategy.py --json
```

**Example `--json` output for `validate`:**
```json
{
  "ok": false,
  "valid": false,
  "errors": [
    {
      "type": "InvalidMethodCall",
      "message": "Call to undefined method 'self.get_volume()'. Not in BaseStrategy interface.",
      "file": "./strategy.py",
      "line": 22
    }
  ],
  "warnings": [
    {
      "type": "NetworkImport",
      "message": "Import 'requests' detected. Network calls from strategies are not supported.",
      "file": "./strategy.py",
      "line": 3
    }
  ]
}
```

### Backtest

```bash
# Run a backtest
agenttrader backtest ./strategy.py --from 2024-01-01 --to 2024-12-31
agenttrader backtest ./strategy.py --from 2024-01-01 --to 2024-12-31 --cash 10000
agenttrader backtest ./strategy.py --from 2024-01-01 --to 2024-12-31 --json

# View results
agenttrader backtest list
agenttrader backtest list --json
agenttrader backtest show <run_id>
agenttrader backtest show <run_id> --json
```

**Example `--json` output for `backtest show`:**
```json
{
  "ok": true,
  "run_id": "b3f2a1c0-4d5e-4f6a-8b9c-0d1e2f3a4b5c",
  "strategy_path": "./momentum_strategy.py",
  "start_date": "2024-01-01",
  "end_date": "2024-12-31",
  "initial_cash": 10000.00,
  "final_value": 13240.50,
  "status": "complete",
  "metrics": {
    "total_return_pct": 32.41,
    "annualized_return_pct": 32.41,
    "sharpe_ratio": 1.84,
    "sortino_ratio": 2.31,
    "max_drawdown_pct": -12.3,
    "win_rate": 0.61,
    "profit_factor": 2.14,
    "calmar_ratio": 2.63,
    "total_trades": 47,
    "avg_slippage": 0.008
  },
  "equity_curve": [
    {"timestamp": 1704067200, "value": 10000.00},
    {"timestamp": 1704153600, "value": 10124.50}
  ],
  "trades": [
    {
      "id": "t-001",
      "market_id": "0xabc...",
      "market_title": "Will X happen?",
      "action": "buy",
      "side": "yes",
      "contracts": 50,
      "price": 0.42,
      "slippage": 0.006,
      "filled_at": 1704070000,
      "pnl": null
    }
  ]
}
```

### Paper Trading

```bash
# Start paper trading (daemon by default)
agenttrader paper start ./strategy.py
agenttrader paper start ./strategy.py --cash 5000
agenttrader paper start ./strategy.py --no-daemon      # blocking, for testing/CI

# Manage running strategies
agenttrader paper list
agenttrader paper list --json
agenttrader paper status <portfolio_id>
agenttrader paper status <portfolio_id> --json
agenttrader paper stop <portfolio_id>
agenttrader paper stop --all
```

**Example `--json` output for `paper status`:**
```json
{
  "ok": true,
  "portfolio_id": "p-7f3a2b1c",
  "strategy_path": "./momentum_strategy.py",
  "status": "running",
  "pid": 84231,
  "started_at": 1704067200,
  "initial_cash": 10000.00,
  "cash_balance": 8234.50,
  "portfolio_value": 11820.30,
  "unrealized_pnl": 1585.80,
  "positions": [
    {
      "market_id": "0xabc...",
      "market_title": "Will Fed cut rates in March?",
      "platform": "polymarket",
      "side": "yes",
      "contracts": 200,
      "avg_cost": 0.41,
      "current_price": 0.63,
      "unrealized_pnl": 440.00
    }
  ],
  "last_reload": 1704153600,
  "reload_count": 3
}
```

### Dashboard & MCP

```bash
agenttrader dashboard                   # starts localhost:8080
agenttrader dashboard --port 9090       # custom port

agenttrader mcp                         # starts MCP server on stdio
```

---

## MCP Tool Definitions

When `agenttrader mcp` runs, the following tools are available to MCP-compatible agents (Claude Code, Cursor, etc.):

| Tool | Arguments | Returns |
|------|-----------|---------|
| `get_markets` | `platform`, `category`, `tags`, `limit` | List of Market objects |
| `get_price` | `market_id` | Current price + orderbook |
| `get_history` | `market_id`, `days` | List of PricePoint objects |
| `match_markets` | `polymarket_slug` OR `kalshi_ticker` | Matched markets across platforms |
| `run_backtest` | `strategy_path`, `start_date`, `end_date`, `initial_cash` | Backtest run ID + results |
| `get_backtest` | `run_id` | Full results JSON (same schema as CLI) |
| `list_backtests` | _(none)_ | Recent backtest runs |
| `validate_strategy` | `strategy_path` | Validation errors + warnings |
| `start_paper_trade` | `strategy_path`, `initial_cash` | Portfolio ID |
| `get_portfolio` | `portfolio_id` | Current positions + P&L |
| `stop_paper_trade` | `portfolio_id` | Confirmation |
| `list_paper_trades` | _(none)_ | All running portfolios |
| `sync_data` | `days`, `platform`, `market_ids` | Sync summary |

---

## Dome API Endpoint Mapping

`data/dome_client.py` is the **only file** that calls Dome. All other code receives translated internal models. Below is the complete mapping from internal method to Dome API call.

```
DomeClient.get_markets(platform, filters)
  → Polymarket: dome.polymarket.markets.get_markets({status, tags, market_slug, min_volume, limit})
  → Kalshi:     dome.kalshi.markets.get_markets({status, market_ticker, min_volume, limit})

DomeClient.get_market_price(market_id, platform, at_time=None)
  → Polymarket: dome.polymarket.markets.get_market_price({token_id, at_time})
  → Kalshi:     dome.kalshi.markets.get_market_price({ticker, at_time})

DomeClient.get_candlesticks(condition_id, platform, start_time, end_time, interval)
  → Polymarket: dome.polymarket.markets.get_candlesticks({condition_id, start_time, end_time, interval})
  → Kalshi:     dome.kalshi.markets.get_candlesticks({ticker, start_time, end_time, interval})
    interval: 1 = 1min, 60 = 1hr, 1440 = 1day

DomeClient.get_orderbook_snapshots(market_id, platform, start_time, end_time, limit)
  → Polymarket: dome.polymarket.markets.get_orderbooks({token_id, start_time, end_time, limit})
    Note: start_time and end_time are in MILLISECONDS for orderbook endpoints
  → Kalshi:     dome.kalshi.orderbooks.get_orderbooks({ticker, start_time, end_time, limit})

DomeClient.get_matching_markets(polymarket_slug=None, kalshi_ticker=None)
  → dome.matching_markets.get_matching_markets({polymarket_market_slug or kalshi_event_ticker})

DomeClient.get_matching_markets_by_sport(sport, date)
  → dome.matching_markets.get_matching_markets_by_sport({sport, date})
    date format: "YYYY-MM-DD"
    sport values: "nfl" | "nba" | "mlb" | "nhl" | "soccer"
```

**Pagination:** All list endpoints use `pagination_key`. `DomeClient` handles pagination internally — callers always receive the full result set.

**Rate limits:**
- Free tier: 10 QPS / 100 per 10s
- Dev tier: 100 QPS / 500 per 10s
- Pro tier: 300 QPS / 3000 per 10s

The `DomeClient` must implement a rate limiter using `asyncio` + `tenacity` for retry-with-backoff.

---

## Backtesting Engine Design

### Look-Ahead Bias Prevention

The `BacktestContext` maintains a `current_ts` cursor. Any call to `get_history()`, `get_price()`, or `get_orderbook()` through the context is filtered to `timestamp < current_ts`. This is enforced at the context level — strategy code cannot access future data regardless of what it tries.

```
BacktestEngine.run(strategy_class, config)
  ├── Load all price_history and orderbook files for date range from cache
  ├── Build sorted event list (one event per price point per market)
  ├── Instantiate BacktestContext(current_ts=config.start_ts)
  ├── Instantiate strategy_class(context)
  ├── strategy.on_start()
  └── For each event (sorted by timestamp):
        context.advance_time(event.timestamp)        ← moves the time cursor forward
        if event.type == 'price_update':
            strategy.on_market_data(market, price, orderbook)
        if event.type == 'schedule_tick':
            strategy.on_schedule(now, market)
        if event.type == 'resolution':
            context.settle_positions(market_id, outcome)
            strategy.on_resolution(market, outcome, pnl)
        context.record_snapshot()                    ← captures equity curve point
  └── strategy.on_stop()
  └── return context.compile_results()
```

### Fill Model

Orders are filled using the orderbook snapshot at the time of the order. Never use mid-price alone.

```
FillModel.simulate_fill(order, orderbook)
  ├── For MARKET BUY: walk the asks list, consuming levels until order is filled
  │     fill_price = weighted average of consumed ask levels
  │     if insufficient depth: partially fill and set partial=True
  ├── For MARKET SELL: walk the bids list, consuming levels
  ├── For LIMIT BUY: fill only if best_ask <= limit_price at any point in the bar
  └── slippage = fill_price - orderbook.mid
```

---

## Paper Daemon Design

The paper trading daemon (`core/paper_daemon.py`) runs as a background OS process using Python `multiprocessing`. It:

1. Connects to the Dome WebSocket (`wss://ws.domeapi.io/{api_key}`) for real-time price updates
2. On each price event, calls `strategy.on_market_data()`
3. Calls `strategy.on_schedule()` every N minutes (configurable, default 15)
4. Uses `watchdog` to monitor the strategy `.py` file for changes
5. On file change: gracefully stops the current strategy (calls `on_stop()`), reloads the module, re-instantiates, calls `on_start()`, logs the reload with timestamp
6. Writes all position and trade updates to `db.sqlite` after every event
7. Writes its PID to the `paper_portfolios` table so `agenttrader paper stop` can send `SIGTERM`

---

## Dependencies (`pyproject.toml`)

```toml
[project]
name = "agenttrader"
version = "0.1.0"
requires-python = ">=3.12"

[project.scripts]
agenttrader = "agenttrader.cli.main:cli"

[project.dependencies]
dome-api-sdk = ">=1.0.0"        # Dome API Python client
click = ">=8.1"                  # CLI framework
mcp = ">=1.0.0"                  # MCP Python SDK (stdio server)
sqlalchemy = ">=2.0"             # ORM for db.sqlite
alembic = ">=1.13"               # Database migrations
pyyaml = ">=6.0"                 # config.yaml parsing
watchdog = ">=4.0"               # File system monitoring for hot-reload
msgpack = ">=1.0"                # Compressed orderbook file serialization
tenacity = ">=8.0"               # Retry-with-backoff for Dome API calls
fastapi = ">=0.110"              # Dashboard web server
uvicorn = ">=0.29"               # ASGI server for dashboard
websockets = ">=12.0"            # WebSocket client for paper trading daemon
rich = ">=13.0"                  # Human-readable terminal output (non-JSON mode)

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-cov>=5.0",
    "ruff>=0.4",
    "mypy>=1.10",
]
```

---

## Example Strategies

### Binary Mean Reversion (Polymarket)

```python
# strategies/mean_reversion.py
from agenttrader import BaseStrategy

class MeanReversionStrategy(BaseStrategy):
    """
    Buy when price is significantly below its 7-day average.
    Sell when it recovers to the average.
    Only trades BINARY markets on Polymarket.
    """

    def on_start(self):
        self.threshold_buy  = 0.12   # buy if price < avg - 0.12
        self.threshold_sell = 0.04   # sell if price > avg - 0.04
        self.max_position_pct = 0.10 # max 10% of portfolio per position
        self.subscribe(platform="polymarket", category="politics")

    def on_market_data(self, market, price, orderbook):
        if market.market_type.value != "binary":
            return

        history = self.get_history(market.id, lookback_hours=168)
        if len(history) < 24:
            return

        avg = sum(h.yes_price for h in history) / len(history)
        position = self.get_position(market.id)

        if position is None and price < (avg - self.threshold_buy):
            max_spend = self.get_cash() * self.max_position_pct
            contracts = max_spend / price
            self.buy(market.id, contracts)
            self.log(f"BUY {market.title[:40]} price={price:.3f} avg={avg:.3f}")

        elif position and price > (avg - self.threshold_sell):
            self.sell(market.id)
            self.log(f"SELL {market.title[:40]} price={price:.3f} avg={avg:.3f}")

    def on_schedule(self, now, market):
        from datetime import datetime
        hours_left = (market.close_time - now.timestamp()) / 3600
        if hours_left < 4:
            position = self.get_position(market.id)
            if position:
                self.sell(market.id)
                self.log(f"Pre-expiry close: {market.title[:40]} ({hours_left:.1f}h left)")
```

### Cross-Platform Arbitrage

```python
# strategies/arbitrage.py
from agenttrader import BaseStrategy

class CrossPlatformArbitrageStrategy(BaseStrategy):
    """
    Detect price discrepancies for the same event across Polymarket and Kalshi.
    Buy on the cheaper platform when spread > min_spread.
    """

    def on_start(self):
        self.min_spread = 0.035  # minimum 3.5 cent spread
        self.position_size = 100  # contracts per trade
        self.subscribe(platform="all", tags=["sports"])

    def on_market_data(self, market, price, orderbook):
        # Find matching market on other platform
        matches = self.search_markets(market.title, platform=(
            "kalshi" if market.platform.value == "polymarket" else "polymarket"
        ))
        if not matches:
            return

        counterpart = matches[0]
        counter_price = self.get_price(counterpart.id)
        spread = abs(price - counter_price)

        if spread < self.min_spread:
            return

        cheaper_id = market.id if price < counter_price else counterpart.id
        if self.get_position(cheaper_id) is None:
            self.buy(cheaper_id, contracts=self.position_size)
            self.log(
                f"ARB {market.title[:30]} spread={spread:.3f} "
                f"poly={price:.3f} kalshi={counter_price:.3f}"
            )
```

---

## The Ideal Agent Workflow

This is the north star for the product. Every design decision should optimize for this flow completing without human intervention.

```bash
# Session start
agenttrader init
agenttrader config set dome_api_key $DOME_API_KEY

# Research phase
agenttrader markets list --category politics --platform polymarket --json
agenttrader sync --days 90 --platform polymarket --category politics
agenttrader markets history <market_id> --days 60 --json

# Agent writes ./strategy.py based on patterns observed

# Validation
agenttrader validate ./strategy.py --json
# Agent fixes any errors reported in JSON

# Backtesting loop
agenttrader backtest ./strategy.py --from 2024-06-01 --to 2024-12-31 --json
# Agent reads sharpe_ratio, max_drawdown_pct, avg_slippage
# Agent edits ./strategy.py, re-runs backtest
# Repeats until metrics are satisfactory

# Deployment
agenttrader paper start ./strategy.py
# Returns portfolio_id in stdout

# Monitoring
agenttrader paper status <portfolio_id> --json
# Agent reads unrealized_pnl, positions
# Agent can edit ./strategy.py — daemon hot-reloads automatically
```
