# agenttrader — Parquet/DuckDB Migration Engineering Prompt

We are replacing agenttrader's backtest data layer. Instead of syncing data from the Dome API into SQLite, backtesting will now run directly against the Jon Becker prediction market dataset stored as Parquet files. DuckDB is the query engine — it reads Parquet files directly, no persistent database file.

Do not touch the paper trading daemon, LiveContext, or anything in `core/paper_daemon.py`. Do not remove the existing Dome API sync — it stays for live paper trading. This change is backtest data layer only.

---

## Background: Schema Notes

Before writing any code, internalize these critical schema facts. Getting these wrong will produce silent incorrect results.

**Polymarket trades have no price column.**
Price must be derived: `taker_amount / (maker_amount + taker_amount)` when the taker is buying YES. `maker_amount` and `taker_amount` are in raw token units (divide by 1e6 for dollar volume).

**Polymarket trades have no direct market link.**
Trades reference `maker_asset_id` and `taker_asset_id` which are CLOB token IDs. To join trades to markets, match through `clob_token_ids` in the markets table (JSON array — index 0 is the YES token).

**Polymarket trades have a null timestamp column.**
The `timestamp` column in the trades table is typed `null`. Timestamps must be obtained by joining through the `blocks` table on `block_number`.

**Kalshi prices are integers (0–100), not floats (0.0–1.0).**
All Kalshi price columns (`yes_price`, `no_price`, `yes_bid`, `yes_ask`, `last_price`) are in cents. Divide by 100 before returning as agenttrader internal models.

**Kalshi volume is in cents.**
Divide by 100 for dollar volume.

---

## Full Parquet Schemas

### `data/polymarket/markets/`
| Column | Type |
|--------|------|
| id | string |
| condition_id | string |
| question | string |
| slug | string |
| outcomes | string (JSON array e.g. `'["Yes","No"]'`) |
| outcome_prices | string (JSON array e.g. `'["0.45","0.55"]'`) |
| clob_token_ids | string (JSON array — index 0 = YES token ID) |
| volume | double |
| liquidity | double |
| active | bool |
| closed | bool |
| end_date | timestamp[ns, tz=UTC] |
| created_at | timestamp[ns, tz=UTC] |
| market_maker_address | string |
| _fetched_at | timestamp[ns] |

### `data/polymarket/trades/`
| Column | Type |
|--------|------|
| block_number | int64 |
| transaction_hash | string |
| log_index | int64 |
| order_hash | string |
| maker | string |
| taker | string |
| maker_asset_id | string (CLOB token ID) |
| taker_asset_id | string (CLOB token ID) |
| maker_amount | int64 (raw token units) |
| taker_amount | int64 (raw token units) |
| fee | int64 |
| timestamp | null ⚠️ JOIN FROM BLOCKS TABLE |
| _fetched_at | timestamp[ns] |
| _contract | string |

### `data/polymarket/blocks/`
| Column | Type |
|--------|------|
| block_number | int64 |
| timestamp | string (Unix timestamp — CAST to BIGINT) |

### `data/kalshi/markets/`
| Column | Type |
|--------|------|
| ticker | string (primary ID) |
| event_ticker | string |
| market_type | string |
| title | string |
| yes_sub_title | string |
| no_sub_title | string |
| status | string (`"finalized"` = resolved) |
| yes_bid | int64 (cents ÷ 100) |
| yes_ask | int64 (cents ÷ 100) |
| no_bid | int64 (cents ÷ 100) |
| no_ask | int64 (cents ÷ 100) |
| last_price | int64 (cents ÷ 100) |
| volume | int64 (cents ÷ 100) |
| volume_24h | int64 |
| open_interest | int64 |
| result | string (`"yes"` / `"no"` / `""`) |
| created_time | timestamp[ns, tz=UTC] |
| open_time | timestamp[ns, tz=UTC] |
| close_time | timestamp[ns, tz=UTC] |
| _fetched_at | timestamp[ns] |

### `data/kalshi/trades/`
| Column | Type |
|--------|------|
| trade_id | string |
| ticker | string |
| count | int64 |
| yes_price | int64 (cents ÷ 100) |
| no_price | int64 (cents ÷ 100) |
| taker_side | string |
| created_time | timestamp[ns, tz=UTC] |
| _fetched_at | timestamp[ns] |

---

## Step 1 — Add DuckDB Dependency

Add to `pyproject.toml` dependencies:

```toml
duckdb = ">=0.10.0"
```

Run `pip install -e .` and verify:

```python
import duckdb
conn = duckdb.connect()
print(duckdb.__version__)
```

---

## Step 2 — Update `agenttrader init` to Prompt for Dataset Download

In `agenttrader/cli/config.py`, update `init_cmd` to prompt the user after initialization:

```python
import subprocess
import click
from pathlib import Path

DATA_DIR = Path.home() / ".agenttrader" / "data"
DOWNLOAD_URL = "https://s3.jbecker.dev/data.tar.zst"

@click.command("init")
def init_cmd():
    # ... existing init logic (alembic, config.yaml) ...

    # After existing init:
    click.echo("\nHistorical dataset for backtesting")
    click.echo("─" * 40)
    click.echo("The Jon Becker dataset contains trade history for")
    click.echo("thousands of Polymarket and Kalshi markets (2021-present).")
    click.echo()
    click.echo("Download options:")
    click.echo("  [1] Full dataset (~36GB) — complete history, all markets")
    click.echo("  [2] Skip — download later with: agenttrader dataset download")
    click.echo()

    choice = click.prompt("Choice [1/2]", default="2")

    if choice == "1":
        _download_dataset()
    else:
        click.echo("Skipping dataset download.")
        click.echo("Run 'agenttrader dataset download' when ready.")
        click.echo("Until then, backtesting uses Dome API sync data (existing behavior).")
```

Implement `_download_dataset()`:

```python
def _download_dataset():
    """Download and extract the Jon Becker prediction market dataset."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = DATA_DIR / "data.tar.zst"

    click.echo(f"\nDownloading to {DATA_DIR} ...")
    click.echo("This will take a while depending on your connection.\n")

    try:
        import urllib.request

        def reporthook(count, block_size, total_size):
            if total_size > 0:
                percent = min(int(count * block_size * 100 / total_size), 100)
                mb_done = count * block_size / 1024 / 1024
                mb_total = total_size / 1024 / 1024
                click.echo(
                    f"\r  {percent}% ({mb_done:.0f} / {mb_total:.0f} MB)",
                    nl=False
                )

        urllib.request.urlretrieve(DOWNLOAD_URL, archive_path, reporthook)
        click.echo("\n  Download complete.")

    except Exception as e:
        click.echo(f"\nDownload failed: {e}")
        click.echo("Try manually: agenttrader dataset download")
        return

    # Extract with zstd
    click.echo("Extracting...")
    try:
        result = subprocess.run(
            ["tar", "--use-compress-program=unzstd", "-xf", str(archive_path), "-C", str(DATA_DIR)],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            _extract_with_python(archive_path, DATA_DIR)
        else:
            click.echo("Extraction complete.")
    except FileNotFoundError:
        _extract_with_python(archive_path, DATA_DIR)

    # Clean up archive
    archive_path.unlink(missing_ok=True)
    click.echo(f"\nDataset ready at {DATA_DIR}")
    click.echo("Run 'agenttrader dataset verify' to confirm all files are present.")


def _extract_with_python(archive_path: Path, dest: Path):
    """Fallback extraction using Python zstandard library."""
    try:
        import zstandard
        import tarfile
        click.echo("Using Python zstandard for extraction...")
        with open(archive_path, "rb") as fh:
            dctx = zstandard.ZstdDecompressor()
            with dctx.stream_reader(fh) as reader:
                with tarfile.open(fileobj=reader, mode="r|") as tar:
                    tar.extractall(dest)
        click.echo("Extraction complete.")
    except ImportError:
        click.echo("Error: Install zstandard: pip install zstandard")
        raise
```

Add `zstandard` to optional dependencies in `pyproject.toml`:

```toml
[project.optional-dependencies]
dataset = ["zstandard>=0.22"]
```

Also add a standalone `agenttrader dataset` command group with two subcommands:

```bash
agenttrader dataset download   # runs _download_dataset() directly
agenttrader dataset verify     # checks expected directories exist and prints file counts
```

`dataset verify` implementation:

```python
@click.command("verify")
def verify_cmd():
    expected = [
        DATA_DIR / "polymarket" / "markets",
        DATA_DIR / "polymarket" / "trades",
        DATA_DIR / "polymarket" / "blocks",
        DATA_DIR / "kalshi" / "markets",
        DATA_DIR / "kalshi" / "trades",
    ]
    all_ok = True
    for path in expected:
        files = list(path.glob("*.parquet")) if path.exists() else []
        status = "✓" if files else "✗ MISSING"
        click.echo(f"  {status}  {path.relative_to(Path.home())} ({len(files)} parquet files)")
        if not files:
            all_ok = False

    if all_ok:
        click.echo("\nDataset OK. Ready for backtesting.")
    else:
        click.echo("\nDataset incomplete. Run: agenttrader dataset download")
```

**Verify Step 2:**

```bash
agenttrader init
# Walk through prompt, choose [2] to skip download
# Must complete without error

agenttrader dataset verify
# If not downloaded: shows ✗ MISSING for all paths, exits cleanly
# If downloaded: shows ✓ with file counts for all 5 directories
```

---

## Step 3 — Implement `ParquetDataAdapter`

Create `agenttrader/data/parquet_adapter.py`.

This is the only file that knows about the Jon Becker parquet schema. Everything else in agenttrader receives translated internal models.

```python
# agenttrader/data/parquet_adapter.py
# DO NOT import dome_api_sdk here.

import duckdb
from pathlib import Path
from typing import Optional
from agenttrader.data.models import Market, MarketType, Platform, PricePoint, OrderBook, OrderLevel

DATA_DIR = Path.home() / ".agenttrader" / "data"

class ParquetDataAdapter:
    """
    Reads the Jon Becker prediction market parquet dataset via DuckDB.
    Translates raw parquet records into agenttrader internal models.

    Connection is in-memory — parquet files are the source of truth.
    No persistent .duckdb file is created.

    NOTE: Orderbook data is synthetic (derived from recent trades).
    Backtest fills are approximate, not based on real orderbook snapshots.
    """

    def __init__(self, data_dir: Path = None):
        self._data_dir = data_dir or DATA_DIR
        self._conn = duckdb.connect()  # in-memory, no persistent file
        self._polymarket_trades  = str(self._data_dir / "polymarket" / "trades"  / "*.parquet")
        self._polymarket_markets = str(self._data_dir / "polymarket" / "markets" / "*.parquet")
        self._polymarket_blocks  = str(self._data_dir / "polymarket" / "blocks"  / "*.parquet")
        self._kalshi_trades      = str(self._data_dir / "kalshi"     / "trades"  / "*.parquet")
        self._kalshi_markets     = str(self._data_dir / "kalshi"     / "markets" / "*.parquet")

    def is_available(self) -> bool:
        """Returns True if parquet files are present and readable."""
        try:
            path = self._data_dir / "polymarket" / "markets"
            return len(list(path.glob("*.parquet"))) > 0
        except Exception:
            return False
```

### Method: `get_markets`

```python
def get_markets(
    self,
    platform: str = "all",
    category: str = None,
    resolved_only: bool = False,
    min_volume: float = None,
    limit: int = 100,
) -> list[Market]:
```

**Polymarket query:**

The `outcomes` and `outcome_prices` columns are JSON string arrays.
The `clob_token_ids` column is a JSON string array — index 0 is the YES token ID used as the primary market ID.

```sql
SELECT
    json_extract_string(clob_token_ids, '$[0]') AS id,
    condition_id,
    question AS title,
    slug,
    volume,
    liquidity,
    active,
    closed,
    end_date,
    created_at,
    json_extract_string(outcome_prices, '$[0]') AS yes_price_str
FROM '{self._polymarket_markets}'
WHERE 1=1
{f"AND closed = true" if resolved_only else ""}
{f"AND volume >= {min_volume}" if min_volume else ""}
ORDER BY volume DESC
LIMIT {limit}
```

Translation to `Market`:
- `platform` = `Platform.POLYMARKET`
- `market_type` = `MarketType.BINARY`
- `resolved` = value of `closed` column
- `resolution`: if `closed=True` and `yes_price_str == "1.0"` → `"yes"`, if `"0.0"` → `"no"`, else `None`
- `category`: extract from `slug` first segment using a simple lookup dict for known prefixes (e.g. `"will"` → `"politics"`, `"bitcoin"` / `"eth"` → `"crypto"`), default `"other"`
- `scalar_low`, `scalar_high` = `None`
- `tags` = `[]`

**Kalshi query:**

```sql
SELECT
    ticker,
    event_ticker,
    title,
    market_type,
    status,
    yes_bid   / 100.0 AS yes_bid,
    yes_ask   / 100.0 AS yes_ask,
    last_price / 100.0 AS last_price,
    volume    / 100.0 AS volume,
    close_time,
    created_time,
    result
FROM '{self._kalshi_markets}'
WHERE 1=1
{f"AND status = 'finalized'" if resolved_only else ""}
{f"AND volume / 100.0 >= {min_volume}" if min_volume else ""}
ORDER BY volume DESC
LIMIT {limit}
```

Translation to `Market`:
- `id` = `ticker`
- `condition_id` = `event_ticker`
- `platform` = `Platform.KALSHI`
- `market_type` = `MarketType.BINARY` if `market_type == "binary"` else `MarketType.SCALAR`
- `resolved` = `status == "finalized"`
- `resolution` = `result` field directly (`"yes"` / `"no"` / `None` if empty string)
- `category`: extract from `event_ticker` using `regexp_extract(event_ticker, '^([A-Z]+)', 1)` then lowercase

---

### Method: `get_price_history`

```python
def get_price_history(
    self,
    market_id: str,
    platform: Platform,
    start_ts: int,       # Unix timestamp in seconds
    end_ts: int,         # Unix timestamp in seconds
) -> list[PricePoint]:
```

**CRITICAL — Polymarket implementation:**

Two non-obvious joins required:
1. `trades` → `blocks` (for timestamps, because trades.timestamp is null)
2. `trades` → `markets` (to find the YES token ID for this market_id)

```sql
WITH block_times AS (
    SELECT block_number, CAST(timestamp AS BIGINT) AS ts
    FROM '{self._polymarket_blocks}'
),
market_tokens AS (
    SELECT json_extract_string(clob_token_ids, '$[0]') AS yes_token_id
    FROM '{self._polymarket_markets}'
    WHERE json_extract_string(clob_token_ids, '$[0]') = '{market_id}'
       OR condition_id = '{market_id}'
    LIMIT 1
)
SELECT
    bt.ts AS timestamp,
    CASE
        WHEN t.taker_asset_id = (SELECT yes_token_id FROM market_tokens)
        THEN CAST(t.taker_amount AS DOUBLE) / (t.maker_amount + t.taker_amount)
        ELSE CAST(t.maker_amount AS DOUBLE) / (t.maker_amount + t.taker_amount)
    END AS yes_price,
    CAST(t.taker_amount + t.maker_amount AS DOUBLE) / 1e6 AS volume
FROM '{self._polymarket_trades}' t
JOIN block_times bt ON t.block_number = bt.block_number
WHERE (
    t.taker_asset_id = (SELECT yes_token_id FROM market_tokens)
    OR t.maker_asset_id = (SELECT yes_token_id FROM market_tokens)
)
AND bt.ts >= {start_ts}
AND bt.ts <= {end_ts}
ORDER BY bt.ts ASC
```

**Kalshi implementation (straightforward):**

```sql
SELECT
    CAST(EPOCH(created_time) AS BIGINT) AS timestamp,
    yes_price / 100.0 AS yes_price,
    no_price  / 100.0 AS no_price,
    count AS volume
FROM '{self._kalshi_trades}'
WHERE ticker = '{market_id}'
AND EPOCH(created_time) >= {start_ts}
AND EPOCH(created_time) <= {end_ts}
ORDER BY timestamp ASC
```

Translate each row to `PricePoint(timestamp, yes_price, no_price, volume)`.

---

### Method: `get_orderbook_snapshot`

The parquet dataset contains trades, not orderbook snapshots. Synthesize a
synthetic orderbook from recent trades near a given timestamp.

```python
def get_orderbook_snapshot(
    self,
    market_id: str,
    platform: Platform,
    at_ts: int,                    # Unix timestamp in seconds
    lookback_seconds: int = 300,   # use last 5 minutes of trades
) -> OrderBook:
```

Fetch trades in the window `[at_ts - lookback_seconds, at_ts]`, compute
volume-weighted average price, construct a synthetic 3-level orderbook:

```python
def _synthesize_orderbook(
    self, vwap: float, total_volume: float, market_id: str, at_ts: int
) -> OrderBook:
    """
    Construct synthetic orderbook from VWAP.
    Spread widens as volume decreases (illiquid markets get wider spreads).
    """
    spread = max(0.005, min(0.03, 500 / (total_volume + 1)))
    half = spread / 2

    bids = [
        OrderLevel(price=round(vwap - half,     4), size=total_volume * 0.4),
        OrderLevel(price=round(vwap - half * 2, 4), size=total_volume * 0.3),
        OrderLevel(price=round(vwap - half * 3, 4), size=total_volume * 0.2),
    ]
    asks = [
        OrderLevel(price=round(vwap + half,     4), size=total_volume * 0.4),
        OrderLevel(price=round(vwap + half * 2, 4), size=total_volume * 0.3),
        OrderLevel(price=round(vwap + half * 3, 4), size=total_volume * 0.2),
    ]
    return OrderBook(
        market_id=market_id,
        timestamp=at_ts,
        bids=bids,
        asks=asks,
    )
```

If no trades are found in the lookback window, return an orderbook with
best_bid=0.49 and best_ask=0.51 (neutral placeholder) and log a warning.

**Verify Step 3:**

```python
from agenttrader.data.parquet_adapter import ParquetDataAdapter
from agenttrader.data.models import Platform

adapter = ParquetDataAdapter()

if not adapter.is_available():
    print("Dataset not downloaded — skipping test")
else:
    # Test Polymarket markets
    markets = adapter.get_markets(platform="polymarket", limit=5)
    assert len(markets) > 0
    assert all(hasattr(m, 'id') for m in markets)
    print(f"Polymarket markets: {len(markets)}")
    print(f"First: {markets[0].title} vol={markets[0].volume:.0f}")

    # Test resolved markets
    resolved = adapter.get_markets(platform="polymarket", resolved_only=True, limit=10)
    assert all(m.resolved for m in resolved), "resolved_only returned non-resolved markets"
    print(f"Resolved markets: {len(resolved)}")

    # Test price history — prices must be in 0.0–1.0 range
    m = markets[0]
    history = adapter.get_price_history(m.id, Platform.POLYMARKET, 1700000000, 1710000000)
    print(f"Price points for {m.title[:30]}: {len(history)}")
    if history:
        assert 0.0 <= history[0].yes_price <= 1.0, f"Price out of range: {history[0].yes_price}"

    # Test Kalshi
    kalshi = adapter.get_markets(platform="kalshi", limit=5)
    assert all(0.0 <= (m.volume or 0) for m in kalshi)
    print(f"Kalshi markets: {len(kalshi)}")

    # Test orderbook synthesis
    ob = adapter.get_orderbook_snapshot(m.id, Platform.POLYMARKET, 1705000000)
    assert ob.best_bid is not None
    assert ob.best_ask is not None
    assert ob.best_ask > ob.best_bid
    print(f"Orderbook mid: {ob.mid:.3f}, spread: {ob.best_ask - ob.best_bid:.4f}")

    print("\nParquetDataAdapter OK")
```

---

## Step 4 — Update `BacktestEngine` to Use `ParquetDataAdapter`

In `agenttrader/core/backtest_engine.py`, update the constructor to accept
either `ParquetDataAdapter` or the existing `DataCache`:

```python
class BacktestEngine:
    def __init__(self, data_source, orderbook_store=None):
        """
        data_source: ParquetDataAdapter (preferred) or DataCache (fallback)
        orderbook_store: only used when data_source is DataCache (legacy)
        """
        self._data = data_source
        self._ob_store = orderbook_store  # None when using ParquetDataAdapter
        self._fill_model = FillModel()
```

Update the `run()` method's data loading step:

```python
def run(self, strategy_class, config: BacktestConfig) -> dict:
    from agenttrader.data.parquet_adapter import ParquetDataAdapter
    from agenttrader.data.cache import DataCache

    using_parquet = isinstance(self._data, ParquetDataAdapter)

    if using_parquet:
        markets = self._data.get_markets(platform="all", limit=10000)
    else:
        # Existing behavior — load from SQLite cache
        markets = self._data.get_markets()

    # Rest of engine loop unchanged
    ...
```

Update `BacktestContext.get_orderbook()` to route to the correct source:

```python
def get_orderbook(self, market_id: str) -> OrderBook:
    if self._parquet_adapter:
        return self._parquet_adapter.get_orderbook_snapshot(
            market_id,
            self._platform_map.get(market_id, Platform.POLYMARKET),
            self._current_ts,
        )
    else:
        # Existing SQLite/orderbook_store path
        return self._ob_store.get_nearest(...)
```

Add `"data_source"` field to the results dict returned by `compile_results()`:

```python
results["data_source"] = "parquet" if using_parquet else "sqlite"
```

**Verify Step 4:**

```python
from agenttrader.core.backtest_engine import BacktestEngine, BacktestConfig
from agenttrader.core.base_strategy import BaseStrategy
from agenttrader.data.parquet_adapter import ParquetDataAdapter

class SimpleTestStrategy(BaseStrategy):
    def on_start(self):
        self.subscribe(platform="polymarket")
    def on_market_data(self, market, price, orderbook):
        if price < 0.4 and self.get_position(market.id) is None:
            if self.get_cash() > 100:
                self.buy(market.id, contracts=10)
        elif price > 0.6 and self.get_position(market.id):
            self.sell(market.id)

adapter = ParquetDataAdapter()
if adapter.is_available():
    engine = BacktestEngine(data_source=adapter)
    results = engine.run(SimpleTestStrategy, BacktestConfig(
        strategy_path="test",
        start_date="2024-01-01",
        end_date="2024-03-31",
        initial_cash=10000.0,
    ))
    assert "metrics" in results
    assert "equity_curve" in results
    assert results["data_source"] == "parquet"
    assert len(results["equity_curve"]) > 0
    print("BacktestEngine with parquet OK")
    print("Metrics:", results["metrics"])
    print("Total trades:", results["metrics"]["total_trades"])
```

---

## Step 5 — Update CLI to Auto-Select Data Source

In `agenttrader/cli/backtest.py`, automatically use `ParquetDataAdapter`
if available, falling back to `DataCache` if not:

```python
from agenttrader.data.parquet_adapter import ParquetDataAdapter
from agenttrader.data.cache import DataCache
from agenttrader.data.orderbook_store import OrderBookStore
from agenttrader.db import get_engine
from agenttrader.core.backtest_engine import BacktestEngine

def get_backtest_engine() -> BacktestEngine:
    """Return engine with best available data source."""
    adapter = ParquetDataAdapter()
    if adapter.is_available():
        return BacktestEngine(data_source=adapter)
    else:
        engine_db = get_engine()
        return BacktestEngine(
            data_source=DataCache(engine_db),
            orderbook_store=OrderBookStore(),
        )
```

When using parquet, add to CLI output:
```
Data source: Jon Becker dataset (parquet) — 2021-present
```

When using fallback:
```
Data source: local sync cache (SQLite) — run 'agenttrader dataset download' for full history
```

Apply the same `get_backtest_engine()` pattern to `agenttrader markets list`
and `agenttrader markets screen` — both should query `ParquetDataAdapter`
when available.

**Verify Step 5:**

```bash
# With dataset downloaded
agenttrader backtest ./strategy.py --from 2024-01-01 --to 2024-03-31 --json | python -c "
import json, sys
data = json.load(sys.stdin)
assert data['ok'] == True
assert data.get('data_source') == 'parquet'
print('data_source: parquet OK')
print('total_trades:', data['metrics']['total_trades'])
"

# Without dataset (simulate by temporarily renaming data dir)
mv ~/.agenttrader/data ~/.agenttrader/data_backup
agenttrader backtest ./strategy.py --from 2024-01-01 --to 2024-03-31 --json | python -c "
import json, sys
data = json.load(sys.stdin)
assert data['ok'] == True
assert data.get('data_source') == 'sqlite'
print('fallback to sqlite OK')
"
mv ~/.agenttrader/data_backup ~/.agenttrader/data

# Markets list uses parquet
agenttrader markets list --platform polymarket --limit 10 --json | python -c "
import json, sys
data = json.load(sys.stdin)
assert data['ok'] == True
assert data.get('data_source') == 'parquet'
print('markets list from parquet OK, count:', data['count'])
"
```

---

## Step 6 — Update `agenttrader sync` Help Text

`agenttrader sync` still works and is still needed for paper trading live
data. Update its help text only — do not change any sync behavior:

```python
@click.command("sync")
def sync_cmd(...):
    """
    Sync live market data from Dome API for paper trading.

    Note: backtesting uses the Jon Becker parquet dataset, not sync data.
    Run 'agenttrader dataset download' to set up the backtest dataset.
    """
```

Also update any user-facing messages that previously implied sync was
required for backtesting.

---

## Final Integration Test

```bash
agenttrader dataset verify

agenttrader markets list --platform polymarket --limit 20 --json | python -c "
import json, sys
data = json.load(sys.stdin)
assert data['ok'] == True
assert len(data['markets']) > 0
assert data.get('data_source') == 'parquet'
print('markets from parquet OK:', data['count'])
"

agenttrader markets screen --condition "current_price < 0.30" --json | python -c "
import json, sys
data = json.load(sys.stdin)
assert data['ok'] == True
print('screener from parquet OK:', data['count'], 'matches')
"

agenttrader backtest ./strategy.py --from 2024-01-01 --to 2024-06-30 --json | python -c "
import json, sys
data = json.load(sys.stdin)
assert data['ok'] == True
assert data.get('data_source') == 'parquet'
m = data['metrics']
print('Backtest OK')
print(f'  Total trades:        {m[\"total_trades\"]}')
print(f'  Sharpe ratio:        {m[\"sharpe_ratio\"]}')
print(f'  Total return:        {m[\"total_return_pct\"]}%')
print(f'  Resolution accuracy: {data[\"resolution_accuracy\"]}')
print(f'  By category:         {data[\"by_category\"]}')
"
```

---

## Constraints

- Do not bump the version number or modify `pyproject.toml` beyond adding `duckdb`
- Do not remove or break any existing paper trading functionality
- Do not touch `core/paper_daemon.py`, `LiveContext`, or the Dome API sync path
- The fallback to SQLite/DataCache must work cleanly when parquet is not available
- `BaseStrategy` interface must not change — existing strategies run unchanged
