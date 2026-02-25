# agenttrader

`agenttrader` is a toolkit that lets AI agents autonomously research, write, backtest, and deploy prediction market trading strategies — without human intervention.

Give an agent a goal. It handles everything: discovering markets on Polymarket and Kalshi, analyzing price history, writing strategy code, running the backtesting loop, and starting a live paper trading daemon. Running strategies hot-reload automatically when the strategy file changes — agents can iterate on live deployments without restarting.

All market data flows through [pmxt](https://github.com/Polymarket/pmxt), unified across Polymarket and Kalshi.

**Primary users:** AI coding agents (Claude Code, Codex, OpenCode, Cursor).  
**Secondary users:** Developers who want to write and test strategies manually.

---

## How It Works

```
Agent receives goal
       │
       ▼
agenttrader sync          ← pull market data into local cache
       │
       ▼
agenttrader markets list  ← discover opportunities
       │
       ▼
[agent writes strategy.py]
       │
       ▼
agenttrader validate      ← catch errors before they waste time
       │
       ▼
agenttrader backtest      ← test against real historical data
       │
       ├─── metrics look bad? agent edits strategy.py, loops back
       │
       ▼
agenttrader paper start   ← deploy as background daemon
       │
       ▼
[agent edits strategy.py] ← daemon hot-reloads, no restart needed
```

All state lives locally in `~/.agenttrader/`. No cloud backend. No app account required. PMXT market data does not require an API key.

---

## Quickstart

### Prerequisites

```bash
python --version    # 3.12+ required
pip --version
```

Install PMXT dependencies (Python package + Node.js sidecar):

```bash
pip install pmxt
npm install -g pmxtjs
```

### Install

```bash
pip install agenttrader
agenttrader init
```

### Verify it works

```bash
agenttrader sync --platform polymarket --days 2 --limit 5 --json
agenttrader markets list --json
```

If you see markets in the output, you're ready.

---

## Using agenttrader With Your Agent

There are two ways to connect your agent to agenttrader. **MCP is recommended** — it's the most natural interface for agents. CLI mode works as a fallback if MCP isn't available.

---

### Option A: MCP (Recommended)

MCP lets your agent call agenttrader tools as native function calls — no shell commands, no output parsing. The agent calls `get_markets()`, `run_backtest()`, `start_paper_trade()` directly inside its reasoning loop.

**Setup for Claude Code**

Add this to `.claude/mcp.json` in your project directory:

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

Restart Claude Code. The MCP server starts automatically — you never run `agenttrader mcp` manually.

**Setup for Cursor**

In Cursor settings → MCP → add server:

```json
{
  "name": "agenttrader",
  "command": "agenttrader",
  "args": ["mcp"],
  "transport": "stdio"
}
```

**Setup for any other MCP-compatible agent**

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

The transport is `stdio`. The agent spawns `agenttrader mcp` as a subprocess and communicates over stdin/stdout. No port, no server to keep running.

**Available MCP tools**

| Category | Tools |
|----------|-------|
| Markets | `get_markets`, `get_price`, `get_history`, `match_markets` |
| Strategy | `validate_strategy`, `run_backtest`, `get_backtest`, `list_backtests` |
| Paper trading | `start_paper_trade`, `get_portfolio`, `stop_paper_trade`, `list_paper_trades` |
| Data | `sync_data` |

**Example agent prompts (MCP mode)**

Once connected, give your agent natural language goals:

```
Use agenttrader to find the 10 most liquid Polymarket politics markets.
Build a mean reversion strategy in ./strategy.py. Backtest it from
2024-06-01 to 2024-12-31 with $10,000 starting cash. Iterate until
Sharpe ratio > 1.0 and max drawdown better than -20%. Then start
paper trading and report the portfolio_id.
```

```
Check my current paper trading portfolio. If any position has been
open for more than 7 days with negative PnL, update the strategy
to add a stop-loss and let me know what changed.
```

```
Find prediction markets on both Polymarket and Kalshi covering the
same event. Write an arbitrage strategy that trades the price
discrepancy when it exceeds 3 cents. Backtest it and paper trade it.
```

The agent will chain tool calls autonomously — syncing data, writing and editing `strategy.py`, running backtests, reading metrics, iterating, and deploying — without you touching a terminal.

---

### Option B: CLI Mode

If your agent can run shell commands (most can), it can use agenttrader without any MCP setup. Every command supports `--json` for clean, parseable output.

**Setup**

No configuration needed beyond the initial install. Your agent runs commands like:

```bash
agenttrader sync --platform polymarket --days 7 --limit 20 --json
agenttrader markets list --platform polymarket --json
agenttrader validate ./strategy.py --json
agenttrader backtest ./strategy.py --from 2024-01-01 --to 2024-06-01 --json
agenttrader paper start ./strategy.py --cash 5000 --json
agenttrader paper status <portfolio_id> --json
```

**Example agent prompts (CLI mode)**

For Claude Code without MCP configured:

```
Use the agenttrader CLI to research Polymarket crypto markets.
Run: agenttrader markets list --platform polymarket --category crypto --json
Then build a strategy in ./strategy.py and iterate using:
  agenttrader validate ./strategy.py --json
  agenttrader backtest ./strategy.py --from 2024-01-01 --to 2024-06-01 --json
Keep iterating until Sharpe > 1.0, then run:
  agenttrader paper start ./strategy.py --json
Report the portfolio_id when done.
```

**The JSON contract**

Every `--json` response follows this shape:

```json
{ "ok": true, "...": "..." }
```

```json
{ "ok": false, "error": "ErrorType", "message": "Human-readable details" }
```

Strategy errors include line numbers and tracebacks so agents can self-heal:

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

---

### The Agent Iteration Loop

Whether using MCP or CLI, the core loop looks like this:

```
1. sync market data
2. inspect markets + history
3. write strategy.py
4. validate --json          → fix any errors
5. backtest --json          → read Sharpe, drawdown, win rate
6. edit strategy.py         → improve based on metrics
7. repeat 4–6 until satisfied
8. paper start              → deploy daemon
9. paper status --json      → monitor
10. edit strategy.py live   → daemon hot-reloads automatically
```

Steps 3–7 are where agents spend most of their time. A well-prompted agent will iterate 5–10 times before deploying, each time reading the structured backtest metrics and making targeted improvements to the strategy logic.

---

## Strategy Authoring

Strategies are plain Python files that subclass `BaseStrategy`. Write them in any editor — or let your agent write them.

```python
from agenttrader import BaseStrategy

class MyStrategy(BaseStrategy):

    def on_start(self):
        # Subscribe to markets — called once at startup
        self.subscribe(platform="polymarket", category="politics")

    def on_market_data(self, market, price, orderbook):
        # Called on every price update for subscribed markets
        history = self.get_history(market.id, lookback_hours=48)
        if len(history) < 2:
            return

        avg = sum(h.yes_price for h in history) / len(history)

        if price < avg - 0.08 and self.get_position(market.id) is None:
            self.buy(market.id, contracts=20)
            self.log(f"BUY {market.title[:40]} @ {price:.3f}")

        elif price > avg + 0.05 and self.get_position(market.id):
            self.sell(market.id)
            self.log(f"SELL {market.title[:40]} @ {price:.3f}")

    def on_schedule(self, now, market):
        # Called every 15 minutes — use for time-based logic
        hours_left = (market.close_time - now.timestamp()) / 3600
        if hours_left < 3 and self.get_position(market.id):
            self.sell(market.id)
            self.log(f"Pre-expiry close: {hours_left:.1f}h left")

    def on_resolution(self, market, outcome, pnl):
        # Called when a market resolves
        self.log(f"Resolved: {outcome}, PnL: ${pnl:.2f}")
```

**Available methods inside a strategy:**

```python
# Subscribe + discover
self.subscribe(platform, category, tags, market_ids)
self.search_markets(query, platform)

# Price + data (time-bounded in backtesting — no look-ahead bias)
self.get_price(market_id)               # current YES price (0.0–1.0)
self.get_orderbook(market_id)           # bids, asks, best_bid, best_ask, mid
self.get_history(market_id, lookback_hours)  # list of PricePoint objects

# Portfolio
self.get_position(market_id)            # open Position or None
self.get_cash()                         # available cash
self.get_portfolio_value()              # cash + mark-to-market positions

# Orders
self.buy(market_id, contracts, side="yes", order_type="market")
self.sell(market_id, contracts=None)    # None = sell entire position

# Utilities
self.log(message)                       # appears in dashboard + paper status
self.set_state(key, value)              # persist state across calls
self.get_state(key, default)
```

Do not call PMXT directly from a strategy. Do not import `requests`, `httpx`, `pmxt`, or any networking library. All data access goes through `self.*` methods.

---

## All Commands

### Setup

```bash
agenttrader init
agenttrader config show
```

### Data Sync

```bash
agenttrader sync                                          # top 100 markets, 90 days
agenttrader sync --platform polymarket --days 7
agenttrader sync --platform kalshi --days 7
agenttrader sync --resolved --days 365 --platform polymarket --json   # resolved/expired markets
agenttrader sync --markets <id1> --markets <id2>          # specific markets
agenttrader sync --json
```

### Markets

```bash
agenttrader markets list
agenttrader markets list --platform polymarket --limit 20 --json
agenttrader markets list --category politics --min-volume 50000
agenttrader markets price <market_id> --json
agenttrader markets history <market_id> --days 30 --json
agenttrader markets screen --condition "current_price < 0.30" --json
agenttrader markets screen --condition "price_vs_7d_avg < -0.10" --platform polymarket --limit 20 --json
agenttrader markets match --polymarket-slug "<slug>"
agenttrader markets match --kalshi-ticker "<ticker>"
```

`markets screen` runs only on local cache data (SQLite) and supports fixed metric expressions:

`price_vs_7d_avg`, `current_price`, `volume`, `days_until_close`, `price_change_24h` with operators `<`, `>`, `<=`, `>=`, `==`.

### Validate

```bash
agenttrader validate ./strategy.py
agenttrader validate ./strategy.py --json
```

### Backtest

```bash
agenttrader backtest ./strategy.py --from 2024-01-01 --to 2024-06-01
agenttrader backtest ./strategy.py --from 2024-01-01 --to 2024-06-01 --cash 10000
agenttrader backtest ./strategy.py --from 2024-01-01 --to 2024-06-01 --json
agenttrader backtest list --json
agenttrader backtest show <run_id> --json
```

Backtest results include:

```json
{
  "metrics": {
    "total_return_pct": 32.4,
    "sharpe_ratio": 1.84,
    "sortino_ratio": 2.31,
    "max_drawdown_pct": -12.3,
    "win_rate": 0.61,
    "profit_factor": 2.14,
    "calmar_ratio": 2.63,
    "avg_slippage": 0.008,
    "total_trades": 47
  },
  "resolution_accuracy": {
    "bought_yes_resolved_yes_pct": 0.64,
    "bought_no_resolved_no_pct": 0.58,
    "sample_size": 47
  },
  "by_category": {
    "politics": { "trades": 23, "win_rate": 0.65, "return_pct": 18.2 },
    "sports": { "trades": 14, "win_rate": 0.43, "return_pct": -4.1 }
  },
  "equity_curve": [...],
  "trades": [...]
}
```

### Paper Trading

```bash
agenttrader paper start ./strategy.py --json
agenttrader paper start ./strategy.py --cash 5000 --json
agenttrader paper start ./strategy.py --no-daemon --json   # blocking, for testing
agenttrader paper list --json
agenttrader paper status <portfolio_id> --json
agenttrader paper compare <portfolio_id_1> <portfolio_id_2> --json
agenttrader paper compare --all --json
agenttrader paper stop <portfolio_id> --json
agenttrader paper stop --all --json
```

**Hot-reload:** edit `strategy.py` while a paper trade is running — the daemon detects the change and reloads automatically. Portfolio state is preserved. No restart needed. `paper status` shows `reload_count` to confirm it happened.

`paper compare` computes side-by-side stats per portfolio from existing SQLite tables (`paper_portfolios`, `positions`, `trades`): portfolio value, unrealized PnL, win rate, avg PnL per sell trade, open positions, and reload count.

### Experiments

```bash
agenttrader experiments log <backtest_run_id> --note "baseline" --tags "politics,mean-reversion" --json
agenttrader experiments log --portfolio <portfolio_id> --note "live run snapshot" --json
agenttrader experiments list --json
agenttrader experiments note <experiment_id> "updated note" --json
agenttrader experiments show <experiment_id> --json
agenttrader experiments compare <exp_id_1> <exp_id_2> --json
```

Experiments are persisted in `~/.agenttrader/experiments.json` and are sorted newest-first in `experiments list`.

### Dashboard

```bash
agenttrader dashboard             # http://localhost:8080
agenttrader dashboard --port 9090
```

Read-only local dashboard showing active paper trades, positions, trade history, strategy logs, and backtest results with equity curves.

Dashboard routes include:

- `#/` overview
- `#/paper` paper portfolios
- `#/compare` side-by-side stats for all running paper portfolios
- `#/backtests` backtest runs
- `#/markets` cached markets

### Maintenance

```bash
agenttrader prune --older-than 90d --dry-run --json
agenttrader prune --older-than 90d --json
```

---

## Local Storage

```
~/.agenttrader/
├── config.yaml        # scheduler and sync preferences
├── db.sqlite          # markets, backtests, positions, trade ledger
├── experiments.json   # experiment memory/log for strategy iterations
└── orderbooks/        # compressed orderbook snapshots by market/day
```

`db.sqlite` is a standard SQLite file. Agents can query it directly with SQL for custom analysis — for example, `SELECT * FROM markets WHERE volume > 10000 ORDER BY volume DESC`.

PMXT market-data calls are unauthenticated; no API key is required for sync, backtest, or paper trading workflows.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `agenttrader: command not found` | Activate your virtual environment: `source .venv/bin/activate` |
| Not initialized | `agenttrader init` |
| Market not in cache | `agenttrader sync ...` |
| Strategy validation errors | `agenttrader validate ./strategy.py --json` — read `errors` array |
| Backtest has no trades | Date range has no price movement — try wider range or sync more markets |
| Dashboard shows blank | Ensure `agenttrader dashboard` is running and open `http://127.0.0.1:8080`; check command logs for startup errors |
| MCP not connecting | Verify `agenttrader mcp` runs without error; check `.claude/mcp.json` path |

---

## Development

```bash
git clone https://github.com/yourname/agenttrader
cd agenttrader
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

ruff check agenttrader tests
python -m compileall agenttrader tests

# Integration tests
python tests/integration/test_full_workflow.py
```

---

## License

MIT
