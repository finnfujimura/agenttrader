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

---

## Get the Historical Dataset (Recommended)

Backtesting works best with the full historical dataset — thousands of resolved Polymarket and Kalshi markets going back to 2021.

```bash
agenttrader dataset download      # ~36GB, one-time
agenttrader dataset build-index   # ~5-10 min, also one-time
```

For the most reliable large dataset download, install `aria2` first. `agenttrader` will automatically use `aria2c` when it is available, and fall back to the built-in Python downloader otherwise.

```bash
# macOS
brew install aria2

# Ubuntu / Debian
sudo apt install aria2
```

Once built, backtests run against the full dataset automatically. No sync required.

> **Don't want to download 36GB?** Backtesting still works using live-synced data. Just run `agenttrader sync` before backtesting and it'll use whatever is cached locally.

---

## Quickstart

### Option A — Use with an AI agent (recommended)

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

Then give your agent a prompt like:

```
Use agenttrader to find liquid Polymarket politics markets,
write a mean reversion strategy, backtest it from 2024-01-01
to 2024-12-31, and iterate until Sharpe > 1.0. Then start
paper trading with $10,000 and report the portfolio ID.
```

The agent handles everything — research, writing, validation, backtesting, iteration, and deployment.

### Option B — Use from the CLI

```bash
# Sync some live data
agenttrader sync --platform polymarket --days 30 --limit 20

# List available markets
agenttrader markets list --platform polymarket --limit 10

# Write a strategy, then validate it
agenttrader validate ./strategy.py

# Backtest it
agenttrader backtest ./strategy.py --from 2024-01-01 --to 2024-12-31

# Start paper trading
agenttrader paper start ./strategy.py --cash 10000
```

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
├── db.sqlite                      # paper trading state
├── data/                          # raw parquet dataset (if downloaded)
├── backtest_index.duckdb          # normalized index (if built)
├── backtest_artifacts/            # compressed backtest results
├── experiments.json               # experiment memory
└── logs/
    └── performance.jsonl
```

---

## Troubleshooting

**`agenttrader: command not found`**
Activate your virtual environment or reinstall: `pip install agenttrader`

**`NotInitialized` error**
Run `agenttrader init`

**Backtest returns 0 trades**
Either your strategy isn't subscribing to any markets, the date range has no data, or the index hasn't been built. Try `agenttrader dataset build-index` if you've downloaded the dataset.

**PMXT import or sidecar errors**
Make sure both are installed: `pip install pmxt` and `npm install -g pmxtjs`

**`DatasetNotFound` when building index**
Run `agenttrader dataset download` first, then `agenttrader dataset build-index`

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
