# agenttrader — Engineering Steps

> Steps are ordered, small, and independently testable. Complete each step fully before moving to the next. Each step ends with a specific test or verification command.

---

## Phase 1 — Project Scaffolding & Storage

### Step 1.1 — Initialize the Python package

Create the full directory structure exactly as specified in `ProjectOverview.md`. Create empty `__init__.py` in every package directory.

Create `pyproject.toml` with all dependencies from the Dependencies section of `ProjectOverview.md`.

The `[project.scripts]` entry must be:
```toml
[project.scripts]
agenttrader = "agenttrader.cli.main:cli"
```

**Verify:**
```bash
pip install -e ".[dev]"
agenttrader --help
# Must print: Usage: agenttrader [OPTIONS] COMMAND [ARGS]...
# Must exit 0
```

---

### Step 1.2 — Implement internal data models

Create `agenttrader/data/models.py` with all dataclasses and enums exactly as specified in the Internal Data Models section of `ProjectOverview.md`:
- `Platform` (Enum)
- `MarketType` (Enum)
- `Market` (dataclass)
- `PricePoint` (dataclass)
- `OrderLevel` (dataclass)
- `OrderBook` (dataclass with `best_bid`, `best_ask`, `mid` properties)
- `Position` (dataclass)
- `FillResult` (dataclass)

No logic in this file — pure data definitions only.

**Verify:**
```python
# Run in Python REPL
from agenttrader.data.models import Market, MarketType, Platform, OrderBook, OrderLevel

m = Market(
    id="abc123",
    condition_id="0xabc",
    platform=Platform.POLYMARKET,
    title="Test",
    category="politics",
    tags=["politics"],
    market_type=MarketType.BINARY,
    volume=10000.0,
    close_time=1800000000,
    resolved=False,
    resolution=None,
    scalar_low=None,
    scalar_high=None,
)
assert m.market_type == MarketType.BINARY
assert m.platform == Platform.POLYMARKET

ob = OrderBook(
    market_id="abc123",
    timestamp=1700000000,
    bids=[OrderLevel(price=0.60, size=100), OrderLevel(price=0.59, size=200)],
    asks=[OrderLevel(price=0.62, size=100), OrderLevel(price=0.63, size=200)],
)
assert ob.best_bid == 0.60
assert ob.best_ask == 0.62
assert abs(ob.mid - 0.61) < 0.001
print("models OK")
```

---

### Step 1.3 — Implement database schema and migrations

Create `agenttrader/db/schema.py` with SQLAlchemy ORM models matching every table in the Database Schema section of `ProjectOverview.md`.

Create `agenttrader/db/__init__.py` with:
```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from pathlib import Path

def get_engine(db_path: Path = None):
    if db_path is None:
        db_path = Path.home() / ".agenttrader" / "db.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{db_path}", echo=False)

def get_session(engine):
    Session = sessionmaker(bind=engine)
    return Session()
```

Initialize Alembic. Generate the initial migration from the ORM models.

**Verify:**
```bash
# From project root
alembic upgrade head
# Should create ~/.agenttrader/db.sqlite with all tables

python -c "
from agenttrader.db import get_engine
from agenttrader.db.schema import Market
from sqlalchemy import inspect
engine = get_engine()
inspector = inspect(engine)
tables = inspector.get_table_names()
required = ['markets', 'price_history', 'backtest_runs', 'paper_portfolios', 'positions', 'trades']
for t in required:
    assert t in tables, f'Missing table: {t}'
print('schema OK:', tables)
"
```

---

### Step 1.4 — Implement `agenttrader init` and `agenttrader config`

Create `agenttrader/cli/main.py` as the Click group entry point:
```python
import click

@click.group()
def cli():
    """agenttrader — prediction market strategy platform"""
    pass
```

Create `agenttrader/cli/config.py` implementing:
- `agenttrader init` — creates `~/.agenttrader/` directory, runs `alembic upgrade head`, writes default `config.yaml`
- `agenttrader config set <key> <value>` — writes to `~/.agenttrader/config.yaml`
- `agenttrader config get <key>` — reads from config
- `agenttrader config show` — prints entire config

Default `config.yaml` contents written by `init`:
```yaml
dome_api_key: ""
schedule_interval_minutes: 15
default_initial_cash: 10000.0
sync_granularity: hourly
max_sync_days: 90
```

All CLI commands must check if `~/.agenttrader/` exists and print a clear error if not:
```
Error: agenttrader not initialized. Run: agenttrader init
```

**Verify:**
```bash
agenttrader init
# Prints: Initialized ~/.agenttrader/

agenttrader config set dome_api_key test_key_123
agenttrader config get dome_api_key
# Prints: test_key_123

agenttrader config show
# Prints full YAML

# Test uninitialized error: rename the directory and confirm error
mv ~/.agenttrader ~/.agenttrader_backup
agenttrader config show
# Must print the "not initialized" error and exit non-zero
mv ~/.agenttrader_backup ~/.agenttrader
```

---

## Phase 2 — Dome API Client

### Step 2.1 — Implement `DomeClient` (read-only market data)

Create `agenttrader/data/dome_client.py`.

`DomeClient` is the **only file in the entire project that imports `dome_api_sdk`**. Enforce this with a comment at the top of every other file: `# DO NOT import dome_api_sdk here. Use agenttrader.data.dome_client only.`

Implement the following methods. Each method calls the Dome API and returns translated internal models (never raw Dome SDK types):

```python
from dome_api_sdk import DomeClient as _DomeSDK
from agenttrader.data.models import Market, MarketType, Platform, PricePoint, OrderBook, OrderLevel
from tenacity import retry, stop_after_attempt, wait_exponential

class DomeClient:
    def __init__(self, api_key: str):
        self._sdk = _DomeSDK({"api_key": api_key})

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get_markets(
        self,
        platform: str = "all",      # 'polymarket' | 'kalshi' | 'all'
        category: str = None,
        tags: list[str] = None,
        market_ids: list[str] = None,
        min_volume: float = None,
        limit: int = 100,
    ) -> list[Market]:
        ...

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get_market_price(
        self,
        market_id: str,
        platform: Platform,
        at_time: int = None,         # Unix timestamp, None = current
    ) -> PricePoint:
        ...

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get_candlesticks(
        self,
        condition_id: str,
        platform: Platform,
        start_time: int,             # Unix timestamp in SECONDS
        end_time: int,               # Unix timestamp in SECONDS
        interval: int = 60,          # 1=1min, 60=1hr, 1440=1day
    ) -> list[PricePoint]:
        ...

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
    def get_orderbook_snapshots(
        self,
        market_id: str,
        platform: Platform,
        start_time: int,             # Unix timestamp in SECONDS (converted to ms internally)
        end_time: int,               # Unix timestamp in SECONDS (converted to ms internally)
        limit: int = 100,
    ) -> list[OrderBook]:
        ...
```

**Critical implementation note for `get_candlesticks` and `get_orderbook_snapshots`:**
- The Dome SDK's candlestick and orderbook endpoints paginate via `pagination_key`. `DomeClient` must loop internally until all pages are fetched, so callers always receive the full dataset.
- The orderbook endpoint uses **milliseconds** for `start_time`/`end_time`. Multiply seconds × 1000 before passing to the SDK.

**Translation logic for `MarketType`:**
```python
def _translate_market_type(dome_market) -> MarketType:
    # Polymarket markets are always BINARY
    # Kalshi: check if market has outcome_type == 'scalar'
    if hasattr(dome_market, 'outcome_type') and dome_market.outcome_type == 'scalar':
        return MarketType.SCALAR
    if hasattr(dome_market, 'outcomes') and len(dome_market.outcomes) > 2:
        return MarketType.CATEGORICAL
    return MarketType.BINARY
```

**Verify** (requires a real Dome API key):
```bash
agenttrader config set dome_api_key $DOME_API_KEY

python -c "
from agenttrader.data.dome_client import DomeClient
from agenttrader.data.models import Platform
import yaml, pathlib

cfg = yaml.safe_load((pathlib.Path.home() / '.agenttrader/config.yaml').read_text())
client = DomeClient(cfg['dome_api_key'])

markets = client.get_markets(platform='polymarket', limit=5)
assert len(markets) > 0
assert all(hasattr(m, 'id') for m in markets)
assert all(hasattr(m, 'market_type') for m in markets)
print(f'Got {len(markets)} markets')
print(f'First: {markets[0].title}')

price = client.get_market_price(markets[0].id, Platform.POLYMARKET)
assert 0.0 <= price.yes_price <= 1.0
print(f'Price: {price.yes_price}')
print('DomeClient OK')
"
```

---

### Step 2.2 — Implement data cache (`cache.py`)

Create `agenttrader/data/cache.py`. This reads/writes markets and price_history to `db.sqlite`.

```python
class DataCache:
    def __init__(self, engine):
        self._engine = engine

    def upsert_market(self, market: Market) -> None:
        """Insert or update a market record."""
        ...

    def upsert_price_point(self, market_id: str, platform: str, point: PricePoint) -> None:
        """Insert a price point, ignoring if timestamp already exists."""
        ...

    def get_markets(
        self,
        platform: str = None,
        category: str = None,
        tags: list[str] = None,
        min_volume: float = None,
        limit: int = 100,
    ) -> list[Market]:
        """Query markets from db.sqlite."""
        ...

    def get_price_history(
        self,
        market_id: str,
        start_ts: int,
        end_ts: int,
    ) -> list[PricePoint]:
        """Get price history filtered by timestamp range."""
        ...

    def get_latest_price(self, market_id: str) -> PricePoint | None:
        """Most recent price point for a market."""
        ...
```

**Verify:**
```python
from agenttrader.db import get_engine
from agenttrader.data.cache import DataCache
from agenttrader.data.models import Market, MarketType, Platform, PricePoint

engine = get_engine()
cache = DataCache(engine)

# Insert a market
m = Market(
    id="test_id_001",
    condition_id="0xtest",
    platform=Platform.POLYMARKET,
    title="Test Market",
    category="test",
    tags=["test"],
    market_type=MarketType.BINARY,
    volume=50000.0,
    close_time=1800000000,
    resolved=False,
    resolution=None,
    scalar_low=None,
    scalar_high=None,
)
cache.upsert_market(m)

# Insert price points
for i, price in enumerate([0.45, 0.46, 0.44, 0.48]):
    cache.upsert_price_point("test_id_001", "polymarket", PricePoint(
        timestamp=1700000000 + (i * 3600),
        yes_price=price,
        no_price=1.0 - price,
        volume=1000.0,
    ))

# Query back
markets = cache.get_markets(platform="polymarket")
assert any(m.id == "test_id_001" for m in markets)

history = cache.get_price_history("test_id_001", 1700000000, 1700015000)
assert len(history) == 4
assert history[0].yes_price == 0.45

latest = cache.get_latest_price("test_id_001")
assert latest.yes_price == 0.48
print("cache OK")
```

---

### Step 2.3 — Implement orderbook file store (`orderbook_store.py`)

Create `agenttrader/data/orderbook_store.py`. This writes and reads compressed orderbook snapshots to `~/.agenttrader/orderbooks/`.

Storage path pattern: `~/.agenttrader/orderbooks/<platform>/<market_id>/<YYYY-MM-DD>.msgpack.gz`

```python
import gzip
import msgpack
from pathlib import Path
from agenttrader.data.models import OrderBook, OrderLevel

class OrderBookStore:
    def __init__(self, base_path: Path = None):
        if base_path is None:
            base_path = Path.home() / ".agenttrader" / "orderbooks"
        self.base_path = base_path

    def write(self, platform: str, market_id: str, snapshots: list[OrderBook]) -> None:
        """
        Group snapshots by calendar day and write one file per day.
        Appends to existing file if it already exists (deduplicates by timestamp).
        """
        ...

    def read(
        self,
        platform: str,
        market_id: str,
        start_ts: int,
        end_ts: int,
    ) -> list[OrderBook]:
        """
        Read snapshots for a market within a timestamp range.
        Loads only the day files needed (based on start_ts, end_ts).
        Returns list sorted by timestamp ascending.
        """
        ...

    def get_nearest(self, platform: str, market_id: str, ts: int) -> OrderBook | None:
        """
        Get the most recent orderbook snapshot at or before timestamp ts.
        Used by BacktestContext to get orderbook for fill simulation.
        """
        ...
```

**Serialization format** (each snapshot serialized as a dict):
```python
{
    "ts": 1700000000,
    "bids": [[0.60, 100.0], [0.59, 200.0]],   # [price, size] pairs
    "asks": [[0.62, 100.0], [0.63, 200.0]],
}
```

**Verify:**
```python
from agenttrader.data.orderbook_store import OrderBookStore
from agenttrader.data.models import OrderBook, OrderLevel
import tempfile, pathlib

store = OrderBookStore(base_path=pathlib.Path(tempfile.mkdtemp()))

snapshots = [
    OrderBook(
        market_id="test_market",
        timestamp=1700000000 + (i * 3600),
        bids=[OrderLevel(price=0.60 - (i * 0.01), size=100.0)],
        asks=[OrderLevel(price=0.62 - (i * 0.01), size=100.0)],
    )
    for i in range(24)
]

store.write("polymarket", "test_market", snapshots)

results = store.read("polymarket", "test_market", 1700000000, 1700090000)
assert len(results) == 24
assert results[0].timestamp == 1700000000

nearest = store.get_nearest("polymarket", "test_market", 1700007000)
assert nearest.timestamp == 1700007200  # closest without going over
print("orderbook_store OK")
```

---

### Step 2.4 — Implement `agenttrader sync`

Create `agenttrader/cli/sync.py`. This is the main data-fetching command.

```bash
agenttrader sync [--days N] [--platform STR] [--category STR] [--markets ID [ID ...]] [--granularity hourly|minute] [--json]
```

Implementation:
1. Load config, instantiate `DomeClient` and `DataCache`
2. Fetch market list (respecting `--platform`, `--category`, `--markets` filters)
3. For each market: fetch candlesticks via `DomeClient.get_candlesticks()` for the date range, write to `DataCache`
4. For each market: fetch orderbook snapshots via `DomeClient.get_orderbook_snapshots()`, write to `OrderBookStore`
5. Update `markets.last_synced` timestamp in SQLite
6. Output progress to stderr (human mode) or collect stats for JSON output

Candlestick interval mapping:
```python
interval_map = {
    "hourly": 60,   # 60-minute candles
    "minute": 1,    # 1-minute candles
    "daily": 1440,  # daily candles
}
```

**Verify:**
```bash
agenttrader sync --platform polymarket --days 7 --json
# Must output valid JSON matching this schema:
# {
#   "ok": true,
#   "markets_synced": <int>,
#   "price_points_fetched": <int>,
#   "orderbook_files_written": <int>,
#   "disk_used_mb": <float>,
#   "errors": []
# }

# Verify data was stored
python -c "
from agenttrader.db import get_engine
from agenttrader.data.cache import DataCache
engine = get_engine()
cache = DataCache(engine)
markets = cache.get_markets(platform='polymarket')
print(f'Markets in cache: {len(markets)}')
assert len(markets) > 0
# Check price history exists for first market
history = cache.get_price_history(markets[0].id, 0, 9999999999)
print(f'Price points for {markets[0].title[:30]}: {len(history)}')
assert len(history) > 0
"
```

---

## Phase 3 — BaseStrategy & Execution Context

### Step 3.1 — Implement `BaseStrategy`

Create `agenttrader/core/base_strategy.py` exactly as specified in the BaseStrategy Interface section of `ProjectOverview.md`.

Do not add any logic here. This is an abstract class only. All method bodies (except the abstract `on_market_data`) should have a `pass` body or a docstring and `pass`.

**Verify:**
```python
from agenttrader.core.base_strategy import BaseStrategy

# Cannot instantiate directly
try:
    BaseStrategy(context=None)
    assert False, "Should have raised TypeError"
except TypeError:
    pass

# Can subclass with on_market_data
class TestStrategy(BaseStrategy):
    def on_market_data(self, market, price, orderbook):
        pass

# Cannot instantiate without context
try:
    s = TestStrategy(context=None)
    print("TestStrategy instantiated OK (context=None allowed in test)")
except Exception as e:
    print(f"Error: {e}")

print("BaseStrategy OK")
```

---

### Step 3.2 — Implement `ExecutionContext`

Create `agenttrader/core/context.py`. `ExecutionContext` is the bridge between a running strategy and the platform's data/portfolio layer.

There are two concrete implementations that share this interface:
- `BacktestContext` — all data reads are time-bounded to `current_ts`
- `LiveContext` — reads from live cache + real-time WebSocket feed

**Interface:**
```python
from abc import ABC, abstractmethod
from agenttrader.data.models import Market, OrderBook, Position, PricePoint

class ExecutionContext(ABC):
    @abstractmethod
    def subscribe(self, platform, category, tags, market_ids) -> None: ...

    @abstractmethod
    def search_markets(self, query, platform) -> list[Market]: ...

    @abstractmethod
    def get_price(self, market_id: str) -> float: ...

    @abstractmethod
    def get_orderbook(self, market_id: str) -> OrderBook: ...

    @abstractmethod
    def get_history(self, market_id: str, lookback_hours: int) -> list[PricePoint]: ...

    @abstractmethod
    def get_position(self, market_id: str) -> Position | None: ...

    @abstractmethod
    def get_cash(self) -> float: ...

    @abstractmethod
    def get_portfolio_value(self) -> float: ...

    @abstractmethod
    def buy(self, market_id, contracts, side, order_type, limit_price) -> str: ...

    @abstractmethod
    def sell(self, market_id, contracts) -> str: ...

    @abstractmethod
    def log(self, message: str) -> None: ...

    @abstractmethod
    def set_state(self, key: str, value) -> None: ...

    @abstractmethod
    def get_state(self, key: str, default=None): ...
```

`BacktestContext` must additionally implement:
```python
def advance_time(self, ts: int) -> None:
    """Move the time cursor forward. Called by BacktestEngine."""
    assert ts >= self._current_ts, f"Time must advance: {ts} < {self._current_ts}"
    self._current_ts = ts

def record_snapshot(self) -> None:
    """Record current portfolio value for equity curve."""
    ...

def settle_positions(self, market_id: str, outcome: str) -> float:
    """Settle all positions for a resolved market. Returns total P&L."""
    ...

def compile_results(self) -> dict:
    """Compile full backtest results dict after engine finishes."""
    ...
```

**Verify:**
```python
from agenttrader.core.context import BacktestContext
from agenttrader.data.models import Platform

# BacktestContext must enforce time boundary
# Build minimal context with fake data
ctx = BacktestContext(
    initial_cash=10000.0,
    price_data={"mkt1": [{"timestamp": 1700000000, "yes_price": 0.5, "volume": 1000}]},
    orderbook_data={},
    markets={},
)
ctx.advance_time(1700000000)

# Should return data at or before current_ts
history = ctx.get_history("mkt1", lookback_hours=24)
assert len(history) == 1

# advance_time should reject going backwards
try:
    ctx.advance_time(1699999999)
    assert False, "Should have raised AssertionError"
except AssertionError:
    pass

print("BacktestContext time boundary OK")
```

---

### Step 3.3 — Implement `FillModel`

Create `agenttrader/core/fill_model.py`.

```python
from agenttrader.data.models import OrderBook, FillResult

class FillModel:
    def simulate_buy(
        self,
        contracts: float,
        orderbook: OrderBook,
        order_type: str = "market",
        limit_price: float = None,
    ) -> FillResult:
        """
        Walk the asks list to fill a buy order.

        Market order:
          Walk asks from lowest to highest price.
          Consume levels until contracts are filled.
          fill_price = weighted average of consumed ask levels.
          If total ask depth < contracts: partial fill.

        Limit order:
          Only fill if best_ask <= limit_price.
          Fill at limit_price (conservative), not at best_ask.
        """
        ...

    def simulate_sell(
        self,
        contracts: float,
        orderbook: OrderBook,
        order_type: str = "market",
        limit_price: float = None,
    ) -> FillResult:
        """
        Walk the bids list to fill a sell order.

        Market order:
          Walk bids from highest to lowest price.
          Consume levels until contracts are filled.
          fill_price = weighted average of consumed bid levels.

        Limit order:
          Only fill if best_bid >= limit_price.
        """
        ...
```

**Verify:**
```python
from agenttrader.core.fill_model import FillModel
from agenttrader.data.models import OrderBook, OrderLevel

fill = FillModel()

ob = OrderBook(
    market_id="test",
    timestamp=1700000000,
    bids=[OrderLevel(0.60, 100), OrderLevel(0.59, 200)],
    asks=[OrderLevel(0.62, 100), OrderLevel(0.63, 200)],
)

# Small buy — should fill entirely at best ask
r = fill.simulate_buy(50, ob)
assert r.filled
assert r.fill_price == 0.62
assert r.contracts == 50
assert not r.partial

# Large buy — walks the book
r = fill.simulate_buy(250, ob)
assert r.filled
assert r.partial == False  # 300 total ask depth, 250 requested
expected_price = (100 * 0.62 + 150 * 0.63) / 250
assert abs(r.fill_price - expected_price) < 0.0001

# Buy larger than full book depth — partial fill
r = fill.simulate_buy(500, ob)
assert r.partial
assert r.contracts == 300  # only 300 available

# Limit order — limit price below best ask, should not fill
r = fill.simulate_buy(50, ob, order_type="limit", limit_price=0.61)
assert not r.filled

print("FillModel OK")
```

---

### Step 3.4 — Implement `BacktestEngine`

Create `agenttrader/core/backtest_engine.py`.

```python
from dataclasses import dataclass
from agenttrader.core.base_strategy import BaseStrategy
from agenttrader.core.context import BacktestContext
from agenttrader.core.fill_model import FillModel

@dataclass
class BacktestConfig:
    strategy_path: str
    start_date: str       # ISO format: "2024-01-01"
    end_date: str         # ISO format: "2024-12-31"
    initial_cash: float = 10000.0
    schedule_interval_minutes: int = 15

class BacktestEngine:
    def __init__(self, cache, orderbook_store):
        self._cache = cache
        self._ob_store = orderbook_store
        self._fill_model = FillModel()

    def run(self, strategy_class: type, config: BacktestConfig) -> dict:
        """
        Run a backtest. Returns results dict (same schema as CLI output).

        Steps:
        1. Load price_history from cache for all markets subscribed by strategy
        2. Load orderbook snapshots from orderbook_store
        3. Build sorted event list from all price points across all markets
        4. Insert 'schedule_tick' events every config.schedule_interval_minutes
        5. Insert 'resolution' events for any markets that resolved in date range
        6. Execute event loop (see Backtesting Engine Design in ProjectOverview.md)
        7. Compute all metrics (see Performance Metrics below)
        8. Return results dict
        """
        ...

    def _compute_metrics(self, equity_curve: list[dict], trades: list[dict]) -> dict:
        """
        Compute all 8 performance metrics:
        - total_return_pct
        - annualized_return_pct
        - sharpe_ratio (risk-free rate = 0)
        - sortino_ratio
        - max_drawdown_pct
        - win_rate
        - profit_factor
        - calmar_ratio
        - total_trades
        - avg_slippage
        """
        ...
```

**Critical:** The engine must call `context.advance_time(event.timestamp)` before calling any strategy hook for that event. This is the look-ahead bias guard.

**Verify:**
```python
# Run a backtest against the local cache (requires sync to have run first)
from agenttrader.core.backtest_engine import BacktestEngine, BacktestConfig
from agenttrader.core.base_strategy import BaseStrategy
from agenttrader.db import get_engine
from agenttrader.data.cache import DataCache
from agenttrader.data.orderbook_store import OrderBookStore

class TrivialStrategy(BaseStrategy):
    """Buys every market it sees if price < 0.5, sells if position exists and price > 0.6"""
    def on_start(self):
        self.subscribe(platform="polymarket")
    def on_market_data(self, market, price, orderbook):
        if price < 0.5 and self.get_position(market.id) is None:
            cash = self.get_cash()
            if cash > 100:
                self.buy(market.id, contracts=10)
        elif price > 0.6 and self.get_position(market.id):
            self.sell(market.id)

db = get_engine()
engine = BacktestEngine(DataCache(db), OrderBookStore())
results = engine.run(TrivialStrategy, BacktestConfig(
    strategy_path="trivial",
    start_date="2024-01-01",
    end_date="2024-01-31",
    initial_cash=10000.0,
))

assert "metrics" in results
assert "equity_curve" in results
assert "trades" in results
assert len(results["equity_curve"]) > 0
assert "total_return_pct" in results["metrics"]
assert "sharpe_ratio" in results["metrics"]
assert "max_drawdown_pct" in results["metrics"]
assert "avg_slippage" in results["metrics"]

# Verify no look-ahead: all equity curve points should be monotonically increasing in timestamp
timestamps = [p["timestamp"] for p in results["equity_curve"]]
assert timestamps == sorted(timestamps)

print("BacktestEngine OK — results:", results["metrics"])
```

---

## Phase 4 — CLI: Markets, Validate, Backtest

### Step 4.1 — Implement `agenttrader markets`

Create `agenttrader/cli/markets.py`. Implement the full markets command group:

- `agenttrader markets list [--platform] [--category] [--tags] [--min-volume] [--limit] [--json]`
- `agenttrader markets price <market_id> [--json]`
- `agenttrader markets history <market_id> [--days] [--json]`
- `agenttrader markets match [--polymarket-slug] [--kalshi-ticker] [--json]`

All commands must:
1. Read from local SQLite cache (not Dome API directly)
2. Print human-readable table output by default (use `rich.table`)
3. Print JSON to stdout when `--json` is passed
4. Print errors as JSON when `--json` is passed, including `"ok": false`

JSON output schemas are defined in the CLI Reference section of `ProjectOverview.md`. Follow them exactly.

**Verify:**
```bash
agenttrader markets list --limit 5
# Should print a rich table with columns: ID, Platform, Title, Category, Price, Volume

agenttrader markets list --limit 5 --json | python -c "
import json, sys
data = json.load(sys.stdin)
assert data['ok'] == True
assert 'markets' in data
assert isinstance(data['markets'], list)
print('markets list JSON OK, count:', data['count'])
"

agenttrader markets price $(agenttrader markets list --limit 1 --json | python -c "import json,sys; print(json.load(sys.stdin)['markets'][0]['id'])")
# Should print current price
```

---

### Step 4.2 — Implement `agenttrader validate`

Create `agenttrader/cli/validate.py`.

The validate command statically analyzes a strategy `.py` file without executing it. Use Python's `ast` module (not `exec`).

Checks to implement:
1. **Class definition check:** File must define exactly one class that subclasses `BaseStrategy`
2. **Abstract method check:** Class must implement `on_market_data(self, market, price, orderbook)`
3. **Signature check:** `on_market_data` must accept exactly `(self, market, price, orderbook)`
4. **Forbidden imports check:** Flag any import of `requests`, `httpx`, `aiohttp`, `urllib`, `dome_api_sdk`
5. **Invalid method check:** Flag any call to `self.<method>` where `<method>` is not in the `BaseStrategy` interface

The allowed `self.*` methods (from BaseStrategy):
```python
ALLOWED_SELF_METHODS = {
    "subscribe", "search_markets", "get_price", "get_orderbook",
    "get_history", "get_position", "get_cash", "get_portfolio_value",
    "buy", "sell", "log", "set_state", "get_state",
}
```

Output format must match the JSON schema in the CLI Reference section of `ProjectOverview.md`.

**Verify:**
```bash
# Write a bad strategy file
cat > /tmp/bad_strategy.py << 'EOF'
import requests
from agenttrader import BaseStrategy

class BadStrategy(BaseStrategy):
    def on_market_data(self, market, price, orderbook):
        data = self.get_volume(market.id)  # invalid method
        r = requests.get("https://example.com")  # forbidden
EOF

agenttrader validate /tmp/bad_strategy.py --json | python -c "
import json, sys
data = json.load(sys.stdin)
assert data['ok'] == True
assert data['valid'] == False
assert len(data['errors']) >= 1
assert len(data['warnings']) >= 1
# Verify error identifies the invalid method
errors = [e['type'] for e in data['errors']]
assert 'InvalidMethodCall' in errors
warnings = [w['type'] for w in data['warnings']]
assert 'NetworkImport' in warnings
print('validate JSON OK')
print('Errors:', data['errors'])
print('Warnings:', data['warnings'])
"

# Write a good strategy file
cat > /tmp/good_strategy.py << 'EOF'
from agenttrader import BaseStrategy

class GoodStrategy(BaseStrategy):
    def on_start(self):
        self.subscribe(platform="polymarket")

    def on_market_data(self, market, price, orderbook):
        if price < 0.4:
            self.buy(market.id, contracts=10)
EOF

agenttrader validate /tmp/good_strategy.py --json | python -c "
import json, sys
data = json.load(sys.stdin)
assert data['valid'] == True
assert data['errors'] == []
print('validate good strategy OK')
"
```

---

### Step 4.3 — Implement `agenttrader backtest`

Create `agenttrader/cli/backtest.py`. Implement:

- `agenttrader backtest <strategy_path> [--from DATE] [--to DATE] [--cash FLOAT] [--json]`
- `agenttrader backtest list [--json]`
- `agenttrader backtest show <run_id> [--json]`

Implementation steps for `agenttrader backtest <path>`:
1. Run `validate` check first. If errors exist, print them and exit non-zero
2. Generate a UUID for `run_id`
3. Hash the strategy file: `sha256(open(strategy_path).read())`
4. Insert a `backtest_runs` row with `status='running'`
5. Dynamically import the strategy class from the file:
   ```python
   import importlib.util
   spec = importlib.util.spec_from_file_location("user_strategy", strategy_path)
   module = importlib.util.module_from_spec(spec)
   spec.loader.exec_module(module)
   # Find the BaseStrategy subclass
   strategy_class = next(
       cls for name, cls in inspect.getmembers(module, inspect.isclass)
       if issubclass(cls, BaseStrategy) and cls is not BaseStrategy
   )
   ```
6. Run `BacktestEngine.run(strategy_class, config)`
7. Update `backtest_runs` row with `status='complete'` and `results_json`
8. Print results

If the strategy raises an exception during backtest, catch it, update row with `status='failed'` and `error=<traceback>`, then output:
```json
{
  "ok": false,
  "error": "StrategyError",
  "message": "name 'undefined_var' is not defined",
  "file": "./strategy.py",
  "line": 14,
  "traceback": "Traceback (most recent call last):\n  ..."
}
```

**Verify:**
```bash
# Use the good strategy written in Step 4.2
agenttrader backtest /tmp/good_strategy.py --from 2024-01-01 --to 2024-01-31 --json | python -c "
import json, sys
data = json.load(sys.stdin)
assert data['ok'] == True
assert 'run_id' in data
assert 'metrics' in data
assert 'equity_curve' in data
m = data['metrics']
assert 'total_return_pct' in m
assert 'sharpe_ratio' in m
assert 'max_drawdown_pct' in m
assert 'avg_slippage' in m
print('backtest JSON OK, metrics:', m)
"

# List backtest runs
agenttrader backtest list --json | python -c "
import json, sys
data = json.load(sys.stdin)
assert data['ok'] == True
assert len(data['runs']) > 0
print('backtest list OK, runs:', len(data['runs']))
"

# Show specific run
RUN_ID=$(agenttrader backtest list --json | python -c "import json,sys; print(json.load(sys.stdin)['runs'][0]['id'])")
agenttrader backtest show $RUN_ID --json | python -c "
import json, sys
data = json.load(sys.stdin)
assert data['ok'] == True
assert 'trades' in data
print('backtest show OK')
"
```

---

## Phase 5 — Paper Trading Daemon

### Step 5.1 — Implement `LiveContext`

Create a `LiveContext` class in `agenttrader/core/context.py` that implements `ExecutionContext` for real-time use.

Unlike `BacktestContext`, `LiveContext`:
- Reads prices from the live cache (updated by WebSocket events)
- Writes trades and positions to `db.sqlite` immediately
- Does not have a time boundary — `get_history()` returns all data up to `now`

```python
class LiveContext(ExecutionContext):
    def __init__(self, portfolio_id: str, initial_cash: float, cache: DataCache, ob_store: OrderBookStore):
        self._portfolio_id = portfolio_id
        self._cash = initial_cash
        self._cache = cache
        self._ob_store = ob_store
        self._fill_model = FillModel()
        self._state: dict = {}
        self._logs: list[str] = []
        self._subscriptions: dict = {}   # market_id -> Market
        ...
```

`buy()` and `sell()` in `LiveContext` must:
1. Get current orderbook via `ob_store.get_nearest()`
2. Run `FillModel.simulate_buy/sell()` to get fill price
3. Write a `trades` row to SQLite
4. Write/update a `positions` row to SQLite
5. Update `cash_balance` in `paper_portfolios` table
6. Return trade ID

**Verify:**
```python
# Unit test: mock the database, verify buy() writes correct records
from unittest.mock import MagicMock, patch
from agenttrader.core.context import LiveContext
from agenttrader.data.models import OrderBook, OrderLevel

mock_cache = MagicMock()
mock_ob_store = MagicMock()
mock_ob_store.get_nearest.return_value = OrderBook(
    market_id="mkt1",
    timestamp=1700000000,
    bids=[OrderLevel(0.60, 500)],
    asks=[OrderLevel(0.62, 500)],
)

ctx = LiveContext(
    portfolio_id="test-portfolio",
    initial_cash=10000.0,
    cache=mock_cache,
    ob_store=mock_ob_store,
)

order_id = ctx.buy("mkt1", contracts=100, side="yes")
assert order_id is not None
assert ctx.get_cash() < 10000.0  # cash decreased
position = ctx.get_position("mkt1")
assert position is not None
assert position.contracts == 100
print("LiveContext buy OK, cash remaining:", ctx.get_cash())
```

---

### Step 5.2 — Implement the paper trading daemon (`paper_daemon.py`)

Create `agenttrader/core/paper_daemon.py`.

The daemon runs as a separate OS process (spawned via `multiprocessing.Process`).

```python
import asyncio
import multiprocessing
import signal
import importlib.util
import inspect
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class PaperDaemon:
    def __init__(self, portfolio_id: str, strategy_path: str, initial_cash: float):
        self.portfolio_id = portfolio_id
        self.strategy_path = Path(strategy_path).resolve()
        self.initial_cash = initial_cash

    def start_as_daemon(self) -> int:
        """Spawn daemon process, return its PID."""
        p = multiprocessing.Process(target=self._run, daemon=False)
        p.start()
        return p.pid

    def _run(self):
        """Entry point for the daemon process."""
        # Register SIGTERM handler for graceful shutdown
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        # Load strategy
        self._load_strategy()

        # Set up file watcher
        self._setup_file_watcher()

        # Start WebSocket listener and scheduler
        asyncio.run(self._main_loop())

    def _load_strategy(self):
        """Import strategy class from file, instantiate with LiveContext."""
        ...

    def _setup_file_watcher(self):
        """
        Use watchdog to monitor strategy_path.
        On file change: log the reload, call strategy.on_stop(),
        re-import the module, re-instantiate, call strategy.on_start().
        """
        ...

    async def _main_loop(self):
        """
        Connect to Dome WebSocket, listen for price events.
        On each event: call strategy.on_market_data() for subscribed markets.
        Every schedule_interval_minutes: call strategy.on_schedule() for all subscribed markets.
        """
        ...

    def _handle_shutdown(self, signum, frame):
        """SIGTERM handler: call strategy.on_stop(), flush logs, exit cleanly."""
        ...
```

**WebSocket connection pattern (from Dome SDK):**
```python
from dome_api_sdk import DomeClient, WebSocketOrderEvent

dome = DomeClient({"api_key": api_key})
ws = dome.polymarket.websocket

def on_event(event: WebSocketOrderEvent):
    # Translate to strategy call
    market_id = event.data.market_slug
    price = event.data.price
    if market_id in self._subscriptions:
        market = self._subscriptions[market_id]
        orderbook = self._ob_store.get_nearest("polymarket", market_id, int(time.time()))
        self._strategy.on_market_data(market, price, orderbook)

await ws.connect()
await ws.subscribe(users=[], on_event=on_event)  # subscribe to all price updates
```

**Verify:**
```bash
# Write a minimal test strategy
cat > /tmp/test_paper.py << 'EOF'
from agenttrader import BaseStrategy

class TestPaperStrategy(BaseStrategy):
    def on_start(self):
        self.subscribe(platform="polymarket", limit=3)
        self.log("Strategy started")

    def on_market_data(self, market, price, orderbook):
        self.log(f"Price update: {market.title[:30]} = {price:.3f}")
EOF

# Start in no-daemon mode (blocking) with a timeout
timeout 10 agenttrader paper start /tmp/test_paper.py --no-daemon || true
# Should print at least one "Price update" log line before timeout

# Start as daemon
agenttrader paper start /tmp/test_paper.py --json | python -c "
import json, sys
data = json.load(sys.stdin)
assert data['ok'] == True
assert 'portfolio_id' in data
assert 'pid' in data
print('paper start OK, portfolio_id:', data['portfolio_id'], 'pid:', data['pid'])
" 

# Get portfolio ID from above and check status
agenttrader paper list --json | python -c "
import json, sys
data = json.load(sys.stdin)
assert data['ok'] == True
assert len(data['portfolios']) > 0
portfolio = data['portfolios'][0]
assert portfolio['status'] == 'running'
print('Status:', portfolio['status'])
"
```

---

### Step 5.3 — Implement `agenttrader paper` CLI commands

Create `agenttrader/cli/paper.py`. Implement:

- `agenttrader paper start <strategy_path> [--cash FLOAT] [--no-daemon] [--json]`
- `agenttrader paper stop <portfolio_id> [--json]`
- `agenttrader paper stop --all [--json]`
- `agenttrader paper status <portfolio_id> [--json]`
- `agenttrader paper list [--json]`

`paper start` flow:
1. Run validate check. Exit with JSON error if invalid.
2. Generate portfolio UUID
3. Insert `paper_portfolios` row with `status='running'`
4. If `--no-daemon`: call `PaperDaemon._run()` directly (blocking)
5. Else: call `PaperDaemon.start_as_daemon()`, update `pid` in database

`paper stop` flow:
1. Look up PID from `paper_portfolios` table
2. Send `os.kill(pid, signal.SIGTERM)`
3. Wait up to 5 seconds for process to exit
4. Update `status='stopped'`, `stopped_at=now()` in database

JSON output schemas must match exactly the schemas in the CLI Reference section of `ProjectOverview.md`.

**Verify:**
```bash
PORT_ID=$(agenttrader paper start /tmp/test_paper.py --json | python -c "import json,sys; print(json.load(sys.stdin)['portfolio_id'])")

sleep 3

agenttrader paper status $PORT_ID --json | python -c "
import json, sys
data = json.load(sys.stdin)
assert data['ok'] == True
assert data['status'] == 'running'
assert 'positions' in data
assert 'portfolio_value' in data
print('paper status OK')
"

agenttrader paper stop $PORT_ID --json | python -c "
import json, sys
data = json.load(sys.stdin)
assert data['ok'] == True
print('paper stop OK')
"

agenttrader paper status $PORT_ID --json | python -c "
import json, sys
data = json.load(sys.stdin)
assert data['status'] == 'stopped'
print('daemon stopped correctly')
"
```

---

### Step 5.4 — Implement hot-reload

In `PaperDaemon._setup_file_watcher()`, use `watchdog` to watch `strategy_path`:

```python
class StrategyFileHandler(FileSystemEventHandler):
    def __init__(self, daemon: "PaperDaemon"):
        self._daemon = daemon

    def on_modified(self, event):
        if Path(event.src_path).resolve() == self._daemon.strategy_path:
            self._daemon._reload_strategy()

def _setup_file_watcher(self):
    handler = StrategyFileHandler(self)
    observer = Observer()
    observer.schedule(handler, str(self.strategy_path.parent), recursive=False)
    observer.start()
    self._observer = observer

def _reload_strategy(self):
    self._strategy.on_stop()
    self._load_strategy()   # re-import module, re-instantiate, call on_start()
    self._context.log(f"Strategy reloaded from {self.strategy_path}")
    # Update reload metadata in db
    # db: paper_portfolios.last_reload = now(), reload_count += 1
```

**Verify:**
```bash
PORT_ID=$(agenttrader paper start /tmp/test_paper.py --json | python -c "import json,sys; print(json.load(sys.stdin)['portfolio_id'])")
sleep 2

# Edit the strategy file
cat >> /tmp/test_paper.py << 'EOF'

    def on_schedule(self, now, market):
        self.log(f"Schedule tick for {market.title[:20]}")
EOF

sleep 3

# Check reload_count increased
agenttrader paper status $PORT_ID --json | python -c "
import json, sys
data = json.load(sys.stdin)
assert data.get('reload_count', 0) >= 1, 'Expected at least 1 reload'
print('Hot-reload OK, reload_count:', data['reload_count'])
"

agenttrader paper stop $PORT_ID
```

---

## Phase 6 — MCP Server

### Step 6.1 — Implement the MCP server

Create `agenttrader/mcp/server.py`. Use the `mcp` Python SDK with stdio transport.

```python
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

server = Server("agenttrader")

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="get_markets",
            description="List prediction markets from local cache. Filter by platform, category, tags.",
            inputSchema={
                "type": "object",
                "properties": {
                    "platform": {"type": "string", "enum": ["polymarket", "kalshi", "all"], "default": "all"},
                    "category": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer", "default": 20},
                },
            },
        ),
        # ... define all 13 tools from the MCP Tool Definitions table in ProjectOverview.md
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "get_markets":
        # call DataCache.get_markets() with arguments
        # return as JSON string
        ...
    elif name == "run_backtest":
        # call BacktestEngine.run()
        ...
    # ... handle all tools

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

Every tool must return a `types.TextContent` with the result serialized as JSON matching the same schemas as the `--json` CLI output.

**All 13 tools to implement** (from MCP Tool Definitions in `ProjectOverview.md`):
`get_markets`, `get_price`, `get_history`, `match_markets`, `run_backtest`, `get_backtest`, `list_backtests`, `validate_strategy`, `start_paper_trade`, `get_portfolio`, `stop_paper_trade`, `list_paper_trades`, `sync_data`

**Verify:**
```bash
# Test MCP server starts without error
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' | agenttrader mcp

# Should print a valid JSON-RPC initialize response
# Expected output contains: "result":{"protocolVersion":"2024-11-05",...}
```

To test with Claude Code, add to `.claude/mcp.json`:
```json
{
  "mcpServers": {
    "agenttrader": {
      "command": "agenttrader",
      "args": ["mcp"]
    }
  }
}
```

---

## Phase 7 — Dashboard

### Step 7.1 — Implement dashboard backend

Create `agenttrader/dashboard/server.py` as a FastAPI application.

Endpoints (read-only — no mutations):

```python
GET /api/portfolios            → list of all paper portfolios with status
GET /api/portfolios/{id}       → single portfolio with positions + recent trades
GET /api/portfolios/{id}/logs  → last 100 log entries for this portfolio
GET /api/backtests             → list of all backtest runs
GET /api/backtests/{id}        → full backtest results including equity curve
GET /api/markets               → list of cached markets with latest prices
GET /api/markets/{id}/history  → price history for a market (query param: days)
GET /api/status                → {"version": "0.1.0", "db_size_mb": 42.3, "markets_cached": 100}
```

All responses are JSON. All responses include `"ok": true`.

**Verify:**
```bash
agenttrader dashboard &
sleep 2

curl -s http://localhost:8080/api/status | python -c "
import json, sys
data = json.load(sys.stdin)
assert data['ok'] == True
assert 'version' in data
assert 'markets_cached' in data
print('dashboard API OK:', data)
"

curl -s http://localhost:8080/api/markets | python -c "
import json, sys
data = json.load(sys.stdin)
assert data['ok'] == True
print('markets endpoint OK, count:', len(data.get('markets', [])))
"

kill %1  # stop background dashboard
```

---

### Step 7.2 — Implement dashboard frontend

Create a minimal React frontend in `agenttrader/dashboard/static/`. It must be **pre-built and committed** (the Python package ships the compiled static files — users should not need Node.js).

Views to implement:
1. **Overview** (`/`) — Active paper trades count, total unrealized PnL, recent backtest count
2. **Paper Trades** (`/paper`) — Table of running portfolios with strategy name, start time, portfolio value, unrealized PnL, position count
3. **Portfolio Detail** (`/paper/:id`) — Positions table, trade history table, log viewer (last 100 entries, auto-refreshes every 10s)
4. **Backtests** (`/backtests`) — Table of backtest runs with strategy name, date range, final return %, Sharpe ratio
5. **Backtest Detail** (`/backtests/:id`) — Metrics cards, equity curve chart (use Recharts), trades table
6. **Markets** (`/markets`) — Searchable table of cached markets with platform, category, volume, current price

The frontend polls `/api/*` endpoints every 10 seconds for live data. No WebSocket needed.

**Verify:**
```bash
agenttrader dashboard &
sleep 2
# Open http://localhost:8080 in browser — verify all 6 views load without errors
# Verify portfolio detail page shows trade history
# Verify backtest detail page shows equity curve chart
kill %1
```

---

## Phase 8 — Polish & Integration Testing

### Step 8.1 — Implement `agenttrader prune`

```bash
agenttrader prune --older-than 90d [--dry-run] [--json]
```

Deletes:
- `price_history` rows with `timestamp < now - duration`
- Orderbook files with date in filename older than duration
- `backtest_runs` rows with `completed_at < now - duration` (keep last 10 always)

`--dry-run` prints what would be deleted without deleting.

**Verify:**
```bash
agenttrader prune --older-than 1d --dry-run --json | python -c "
import json, sys
data = json.load(sys.stdin)
assert data['ok'] == True
assert 'price_points_to_delete' in data
assert 'orderbook_files_to_delete' in data
assert 'dry_run' in data and data['dry_run'] == True
print('prune dry-run OK:', data)
"
```

---

### Step 8.2 — Structured error output across all commands

Audit every CLI command. For any command invoked with `--json`, verify that **all** error paths return JSON to stdout:
- Missing API key → `{"ok": false, "error": "ConfigError", "message": "dome_api_key not set. Run: agenttrader config set dome_api_key <key>"}`
- Strategy file not found → `{"ok": false, "error": "FileNotFoundError", "message": "Strategy file not found: ./bad_path.py"}`
- Strategy execution error → includes `"file"`, `"line"`, `"traceback"` fields
- Dome API error → includes `"retry_after"` if rate-limited
- Market not in cache → `{"ok": false, "error": "MarketNotCached", "message": "Market <id> not in cache. Run: agenttrader sync"}`

Create a shared error-handling decorator for all CLI commands:
```python
def json_errors(func):
    """Wrap a CLI command to catch all exceptions and output JSON errors when --json is active."""
    ...
```

**Verify:**
```bash
# Test each error path
agenttrader markets price nonexistent_id --json | python -c "
import json, sys
data = json.load(sys.stdin)
assert data['ok'] == False
assert 'error' in data
assert 'message' in data
print('Error JSON OK:', data)
"

agenttrader backtest /nonexistent/path.py --json | python -c "
import json, sys
data = json.load(sys.stdin)
assert data['ok'] == False
assert data['error'] == 'FileNotFoundError'
print('FileNotFoundError JSON OK')
"
```

---

### Step 8.3 — Integration test: full agent workflow

Write `tests/integration/test_full_workflow.py`. This test simulates the complete agent workflow end-to-end:

```python
import subprocess, json, time, os, tempfile

def run(cmd, **kwargs) -> dict:
    """Run agenttrader CLI command, return parsed JSON output."""
    result = subprocess.run(
        f"agenttrader {cmd} --json",
        shell=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)

def test_full_agent_workflow():
    # 1. Init
    subprocess.run("agenttrader init", shell=True, check=True)

    # 2. Sync data
    r = run("sync --days 7 --platform polymarket --limit 10")
    assert r["ok"]
    assert r["markets_synced"] > 0

    # 3. List markets
    r = run("markets list --limit 5")
    assert r["ok"]
    assert len(r["markets"]) > 0
    market_id = r["markets"][0]["id"]

    # 4. Get price history
    r = run(f"markets history {market_id} --days 7")
    assert r["ok"]
    assert len(r["history"]) > 0

    # 5. Write a strategy
    strategy = tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w")
    strategy.write("""
from agenttrader import BaseStrategy
class IntegrationTestStrategy(BaseStrategy):
    def on_start(self):
        self.subscribe(platform="polymarket")
    def on_market_data(self, market, price, orderbook):
        if price < 0.5 and self.get_position(market.id) is None:
            self.buy(market.id, contracts=10)
        elif price > 0.6 and self.get_position(market.id):
            self.sell(market.id)
""")
    strategy.close()

    # 6. Validate
    r = run(f"validate {strategy.name}")
    assert r["ok"]
    assert r["valid"]

    # 7. Backtest
    r = run(f"backtest {strategy.name} --from 2024-01-01 --to 2024-01-07 --cash 1000")
    assert r["ok"]
    assert "metrics" in r
    assert "equity_curve" in r
    run_id = r["run_id"]

    # 8. Show backtest results
    r = run(f"backtest show {run_id}")
    assert r["ok"]
    assert "trades" in r

    # 9. Start paper trade
    r = run(f"paper start {strategy.name} --cash 1000")
    assert r["ok"]
    portfolio_id = r["portfolio_id"]

    time.sleep(3)

    # 10. Check paper status
    r = run(f"paper status {portfolio_id}")
    assert r["ok"]
    assert r["status"] == "running"

    # 11. Stop paper trade
    r = run(f"paper stop {portfolio_id}")
    assert r["ok"]

    os.unlink(strategy.name)
    print("Full integration test PASSED")

if __name__ == "__main__":
    test_full_agent_workflow()
```

**Verify:**
```bash
python tests/integration/test_full_workflow.py
# Must print: Full integration test PASSED
```

---

## Summary Checklist

| Phase | Step | Description | Test |
|-------|------|-------------|------|
| 1 | 1.1 | Package scaffold | `agenttrader --help` |
| 1 | 1.2 | Data models | Python REPL assertions |
| 1 | 1.3 | DB schema + migrations | `alembic upgrade head` |
| 1 | 1.4 | `init` + `config` | CLI command verification |
| 2 | 2.1 | `DomeClient` | Python REPL with real API key |
| 2 | 2.2 | `DataCache` | Python REPL assertions |
| 2 | 2.3 | `OrderBookStore` | Python REPL assertions |
| 2 | 2.4 | `agenttrader sync` | CLI + DB verification |
| 3 | 3.1 | `BaseStrategy` | Instantiation + subclass test |
| 3 | 3.2 | `ExecutionContext` + `BacktestContext` | Time boundary test |
| 3 | 3.3 | `FillModel` | Orderbook walk assertions |
| 3 | 3.4 | `BacktestEngine` | End-to-end backtest run |
| 4 | 4.1 | `markets` CLI | JSON schema verification |
| 4 | 4.2 | `validate` CLI | Good + bad strategy tests |
| 4 | 4.3 | `backtest` CLI | Full backtest + list + show |
| 5 | 5.1 | `LiveContext` | Mock buy/sell test |
| 5 | 5.2 | `PaperDaemon` | `--no-daemon` mode test |
| 5 | 5.3 | `paper` CLI | Start + status + stop flow |
| 5 | 5.4 | Hot-reload | File edit triggers reload |
| 6 | 6.1 | MCP server | JSON-RPC initialize response |
| 7 | 7.1 | Dashboard API | curl endpoint tests |
| 7 | 7.2 | Dashboard frontend | Browser smoke test |
| 8 | 8.1 | `prune` command | Dry-run JSON output |
| 8 | 8.2 | Error JSON | All error paths return `ok: false` |
| 8 | 8.3 | Integration test | Full workflow script passes |
