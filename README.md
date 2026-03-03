# agenttrader

A toolkit for AI agents to research, backtest, and paper trade prediction market strategies on Polymarket and Kalshi — autonomously, from a single prompt.

Works as an MCP server (for Claude Code, Cursor, Codex) or directly from the CLI.

---

## Install

```bash
pip install agenttrader
npm install -g pmxtjs
```

Then initialize:

```bash
agenttrader init
```

That's it. `init` sets up your local database and walks you through the optional dataset download.

### Downloading the historical dataset (recommended)

Backtesting works best with the full dataset — thousands of resolved Polymarket and Kalshi markets going back to 2021.

```bash
# Optional but highly recommended:

# aria2 → faster + more reliable large downloads
# zstd  → required to validate/extract .tar.zst archives using system tools

# macOS
brew install aria2 zstd

# Windows (Chocolatey)
choco install aria2 zstandard

# Linux (Debian/Ubuntu)
sudo apt install aria2 zstd

agenttrader dataset download      # ~36GB, one-time
agenttrader dataset build-index   # ~5-10 min, one-time
```

> **Don't want to download 36GB?** Backtesting still works using live-synced data. Run `agenttrader sync` before backtesting and it'll use whatever is cached locally.
---

## Quickstart

### 1. Set up the MCP server

Add to your MCP config (e.g. `.claude/mcp.json`):

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

### 2. Give your agent a prompt

```
Use agenttrader to find liquid Polymarket politics markets,
write a mean reversion strategy, backtest it from 2024-01-01
to 2024-12-31, and iterate until Sharpe > 1.0. Then start
paper trading with $10,000 and report the portfolio ID.
```

The agent handles research, strategy writing, validation, backtesting, iteration, and deployment.

---

## Core Workflow

This is the loop an agent (or human) follows when using agenttrader. Every step maps to an MCP tool call.

```
 research_markets          Find markets with price analytics and capabilities
        │
        ▼
 Write strategy.py         Python class extending BaseStrategy
        │
        ▼
 validate_and_backtest     Validate syntax + run backtest in one call
        │
        ▼
 Evaluate metrics          Sharpe, return %, max drawdown, win rate
        │
   ┌────┴────┐
   │ Good?   │
   │  No ────┼──▶ Edit strategy, re-run backtest
   │  Yes    │
   └────┬────┘
        ▼
 start_paper_trade         Deploy to live paper trading
        │
        ▼
 get_portfolio             Monitor positions and P&L
```

### Example: full autonomous session

Here's a single prompt that exercises the complete workflow:

```
I want to trade Polymarket politics markets using a contrarian strategy.

1. Use research_markets to find 10 active politics markets with 30 days
   of history. Check their capabilities to confirm they're backtestable.

2. Write a strategy that buys YES when the price drops more than 10%
   below its 7-day average, and sells when it recovers to the average.
   Use position sizing of 5% of portfolio per trade.

3. Backtest from 2024-01-01 to 2024-12-31 with $10,000.

4. If Sharpe < 0.5, adjust the entry threshold and retest. Iterate
   up to 3 times.

5. Once satisfied, start paper trading with the best-performing version
   and report the portfolio ID and initial positions.
```

This prompt will trigger ~10-15 tool calls across research, validation, backtesting (with iteration), and paper trading deployment.

---

## Data Architecture

```
              ┌──────────────┐
              │   PMXT API   │  Live prices, orderbooks, candles
              └──────┬───────┘
                     │ sync_data
                     ▼
              ┌──────────────┐
              │ SQLite Cache │  Paper trading, live lookups
              │  db.sqlite   │
              └──────────────┘

   ┌─────────────────┐          ┌──────────────────┐
   │ Parquet Dataset  │  build   │  DuckDB Index    │
   │ ~36GB raw files  │────────▶│  ~13GB normalized │  Backtesting (fastest)
   │ data/poly+kalshi │  index   │  backtest_index   │
   └─────────────────┘          └──────────────────┘
```

Tools automatically select the best available source: **DuckDB index > raw parquet > SQLite cache**. You don't need to specify which source to use.

---

## Market Capabilities

When researching markets, each result includes capability annotations that tell you upfront what's possible:

```json
{
  "capabilities": {
    "backtest": { "index_available": true, "index_start": "2024-06-01", "index_end": "2025-02-15" },
    "history":  { "cache_available": true, "last_point_timestamp": "2026-02-27T18:00:00+00:00" },
    "sync":     { "can_attempt_live_sync": true }
  }
}
```

- **backtest** — whether this market has indexed historical data and the date range covered
- **history** — whether this market has cached price data in SQLite
- **sync** — whether live data sync is possible (false for resolved markets)

This eliminates trial-and-error discovery — agents know immediately which markets can be backtested, which have cached data, and which need a sync first.

---

## Writing a Strategy

Create a Python file with a class that extends `BaseStrategy`:

```python
from agenttrader import BaseStrategy

class MyStrategy(BaseStrategy):

    def on_start(self):
        # Choose which markets to trade
        self.subscribe(platform="polymarket", category="politics")

    def on_market_data(self, market, price, orderbook):
        # Called on every price update for subscribed markets
        history = self.get_history(market.id, lookback_hours=48)
        if len(history) < 2:
            return

        avg = sum(p.yes_price for p in history) / len(history)

        if price < avg - 0.08 and self.get_position(market.id) is None:
            self.buy(market.id, contracts=20)

        elif price > avg + 0.05 and self.get_position(market.id):
            self.sell(market.id)

    def on_schedule(self, now, market):
        # Called on a regular interval (default every 15 min)
        pass

    def on_resolution(self, market, outcome, pnl):
        self.log(f"{market.title} resolved {outcome} — PnL: {pnl:.2f}")
```

**Available methods inside your strategy:**

| Method | What it does |
|---|---|
| `subscribe(platform, category, market_ids)` | Declare which markets to trade |
| `search_markets(query, platform)` | Find markets by keyword |
| `get_price(market_id)` | Current mid price (0.0–1.0) |
| `get_history(market_id, lookback_hours)` | Recent price history |
| `get_orderbook(market_id)` | Live orderbook |
| `get_position(market_id)` | Current open position |
| `get_cash()` | Available cash |
| `get_portfolio_value()` | Total portfolio value |
| `buy(market_id, contracts, side)` | Open a position |
| `sell(market_id)` | Close a position |
| `log(message)` | Append to strategy log |
| `set_state / get_state` | Persist values across ticks |

---

## Backtesting

```bash
# Basic backtest
agenttrader backtest ./strategy.py --from 2024-01-01 --to 2024-12-31

# With JSON output (useful for agents)
agenttrader backtest ./strategy.py --from 2024-01-01 --to 2024-12-31 --json

# View full results (equity curve + trade log)
agenttrader backtest show <run_id> --json
```

Backtest output includes:

- `metrics` — Sharpe, return %, max drawdown, win rate, trade count
- `resolution_accuracy` — did you buy YES when the market actually resolved YES?
- `by_category` — performance breakdown by market category
- `data_source` — which data backend was used

**Optional flags for faster exploratory runs:**

```bash
# Limit to 100 markets
agenttrader backtest ./strategy.py --from 2024-01-01 --to 2024-12-31 --max-markets 100

# Use hourly bars instead of every trade (much faster, less precise)
agenttrader backtest ./strategy.py --from 2024-01-01 --to 2024-12-31 --fidelity bar_1h
```

By default, backtests run on all subscribed markets with full trade fidelity. These flags are opt-in.

### Execution Modes

Backtests support three execution modes that control how trades are filled and whether orderbook data is used:

| Mode | Default? | Fill behavior | Orderbook access |
|------|----------|---------------|------------------|
| `strict_price_only` | **Yes** | Fills at observed price, zero slippage | `get_orderbook()` returns `None` |
| `observed_orderbook` | No | Uses real stored orderbook snapshots | Raises if no observed OB exists |
| `synthetic_execution_model` | No | Synthesizes orderbooks for approximate fill modeling | Always available (modeled) |

**Why strict is the default:** Historical orderbook data is not available in the parquet dataset. Synthetic orderbooks can produce misleadingly optimistic backtest results. `strict_price_only` forces strategies to trade on price signals alone, producing conservative and reproducible results.

```bash
# Default: strict price-only fills
agenttrader backtest ./strategy.py --from 2024-01-01 --to 2024-12-31

# Opt-in to synthetic execution modeling (use with caution)
agenttrader backtest ./strategy.py --from 2024-01-01 --to 2024-12-31 --execution-mode synthetic_execution_model
```

Strategies that call `get_orderbook()` should handle `None` gracefully:

```python
def on_market_data(self, market, price, orderbook):
    if orderbook is not None:
        # Use orderbook-aware logic
        best_bid = orderbook.bids[0].price if orderbook.bids else price
    else:
        # Fall back to price-only logic
        best_bid = price
```

---

## Paper Trading

```bash
# Start a strategy
agenttrader paper start ./strategy.py --cash 10000 --json

# Check status
agenttrader paper status <portfolio_id> --json

# List all running strategies
agenttrader paper list --json

# Stop a strategy
agenttrader paper stop <portfolio_id> --json
agenttrader paper stop --all --json
```

Strategies **hot-reload on file save** — edit your strategy while it's running and it picks up changes within a second.

**Run multiple strategies in parallel and compare them:**

```bash
agenttrader paper compare <portfolio_id_1> <portfolio_id_2> --json
agenttrader paper compare --all --json
```

---

## Experiment Tracking

Log backtest runs as experiments to build memory across sessions:

```bash
# Log a backtest run
agenttrader experiments log <run_id> --note "baseline — threshold 0.10" --tags "politics,mean-reversion"

# Compare two experiments side by side
agenttrader experiments compare <exp_id_1> <exp_id_2> --json

# List all experiments (most recent first)
agenttrader experiments list --json
```

This lets your agent reconstruct the full research history — what was tried, what changed, and whether it helped — across separate sessions.

---

## Market Screener

Find markets matching specific price conditions:

```bash
# Markets significantly below their 7-day average (mean reversion candidates)
agenttrader markets screen --condition "price_vs_7d_avg < -0.10" --json

# Cheap markets with volume
agenttrader markets screen --condition "current_price < 0.25" --min-volume 50000 --json

# Markets closing soon
agenttrader markets screen --condition "days_until_close < 7" --json
```

Supported condition metrics: `price_vs_7d_avg`, `current_price`, `volume`, `days_until_close`, `price_change_24h`

---

## Reference

| Document | Description |
|----------|-------------|
| [COMMANDS.md](COMMANDS.md) | Full MCP tool reference — every tool, parameter, type, default, and example |
| [SCHEMA.md](SCHEMA.md) | Database schemas — SQLite tables, DuckDB index, parquet dataset layout |

---

## CLI Reference

<details>
<summary>Setup</summary>

```bash
agenttrader init
agenttrader config show
agenttrader config set <key> <value>
agenttrader config get <key>
```
</details>

<details>
<summary>Dataset</summary>

```bash
agenttrader dataset verify
agenttrader dataset download
agenttrader dataset build-index [--force] [--json]
```
</details>

<details>
<summary>Sync (live data)</summary>

```bash
agenttrader sync --platform all --days 7 --limit 100
agenttrader sync --resolved --platform polymarket --days 365
agenttrader sync --markets <id1> --markets <id2>
```
</details>

<details>
<summary>Markets</summary>

```bash
agenttrader markets list --platform all --limit 100 --json
agenttrader markets price <market_id> --json
agenttrader markets history <market_id> --days 30 --json
agenttrader markets screen --condition "..." --json
agenttrader markets match --polymarket-slug "..." --json
agenttrader markets match --kalshi-ticker "..." --json
```
</details>

<details>
<summary>Backtests</summary>

```bash
agenttrader validate ./strategy.py --json
agenttrader backtest ./strategy.py --from 2024-01-01 --to 2024-06-01 --json
agenttrader backtest list --json
agenttrader backtest show <run_id> --json
```
</details>

<details>
<summary>Paper trading</summary>

```bash
agenttrader paper start ./strategy.py --cash 10000 --json
agenttrader paper status <portfolio_id> --json
agenttrader paper list --json
agenttrader paper stop <portfolio_id> --json
agenttrader paper compare --all --json
```
</details>

<details>
<summary>Experiments</summary>

```bash
agenttrader experiments log <run_id> --note "..." --tags "..." --json
agenttrader experiments list --json
agenttrader experiments show <exp_id> --json
agenttrader experiments note <exp_id> "updated note" --json
agenttrader experiments compare <exp_id_1> <exp_id_2> --json
```
</details>

---

## Local Storage Layout

```
~/.agenttrader/
├── config.yaml
├── db.sqlite                      # market metadata, paper trading state
├── data/                          # raw parquet dataset (if downloaded)
├── backtest_index.duckdb          # normalized index (if built)
├── backtest_artifacts/            # compressed backtest results
├── experiments.json               # experiment memory
└── logs/
    └── performance.jsonl
```

---

## Development

```bash
git clone https://github.com/finnfujimura/agenttrader
cd agenttrader
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

---

## License

MIT
