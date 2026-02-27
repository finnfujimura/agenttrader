# agenttrader

`agenttrader` is a local toolkit for agent-driven prediction market research, backtesting, and paper trading across Polymarket and Kalshi.

It is built for coding agents first (Codex, Claude Code, Cursor, OpenCode), while still being easy to use directly from the CLI.

## What Changed Recently

- Migrated market connectivity to **PMXT**.
- Added **dataset index build** (`agenttrader dataset build-index`) for faster full-history backtests.
- Added **streaming backtest engine** with lower memory usage.
- Added **artifact-backed backtest storage**:
  - `backtest` returns summary metrics.
  - `backtest show <run_id>` loads full `equity_curve` and `trades` from artifact files.
- Added optional backtest guardrails:
  - `--max-markets`
  - `--fidelity exact_trade|bar_1h|bar_1d`
- Added `sync --resolved` for resolved/expired market syncing.
- Added `resolution_accuracy` and `by_category` to backtest output.
- Added paper trading comparison:
  - `paper compare <id1> <id2>`
  - `paper compare --all`
- Added market screener:
  - `markets screen --condition "..."`
- Added experiment tracking in `~/.agenttrader/experiments.json`.
- Added MCP compound tools (`research_markets`, `validate_and_backtest`) and error `fix` hints.
- Added performance logs for CLI and MCP calls.

## Requirements

- Python 3.12+
- `pip`
- Node.js (required by PMXT sidecar)

## Install

```bash
pip install agenttrader
```

PMXT is installed as a Python dependency, but you still need the Node sidecar:

```bash
npm install -g pmxtjs
```

## Quickstart

```bash
agenttrader init
```

`init` creates `~/.agenttrader`, runs DB migrations, and prompts for optional dataset download.

### Verify local data setup

```bash
agenttrader dataset verify
```

`dataset verify` prefers `./data` if present; otherwise it checks `~/.agenttrader/data`.

### Download historical dataset (recommended for full backtests)

```bash
agenttrader dataset download
agenttrader dataset build-index
```

Use `--force` to rebuild the index:

```bash
agenttrader dataset build-index --force --json
```

### Run a minimal workflow

```bash
agenttrader sync --platform polymarket --days 7 --limit 20 --json
agenttrader markets list --platform polymarket --limit 10 --json
agenttrader validate ./strategy.py --json
agenttrader backtest ./strategy.py --from 2024-01-01 --to 2024-06-01 --json
agenttrader paper start ./strategy.py --cash 10000 --json
```

## Agent-First Usage

### MCP (recommended)

Run agenttrader as an MCP server via stdio:

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

### MCP tools (high-level)

- Markets: `get_markets`, `get_price`, `get_history`, `match_markets`, `research_markets`
- Strategy/backtests: `validate_strategy`, `run_backtest`, `validate_and_backtest`, `get_backtest`, `list_backtests`
- Paper: `start_paper_trade`, `get_portfolio`, `stop_paper_trade`, `list_paper_trades`
- Data: `sync_data`

### MCP behavior improvements

- Structured errors include a `fix` field when possible.
- `get_history` returns analytics by default; raw points are opt-in via `include_raw=true`.
- `research_markets` combines sync + market listing + history fetch.
- `validate_and_backtest` combines validate + backtest.

## CLI Commands

### Setup and config

```bash
agenttrader init
agenttrader config show
agenttrader config set <key> <value>
agenttrader config get <key>
```

### Dataset

```bash
agenttrader dataset verify
agenttrader dataset download
agenttrader dataset build-index [--force] [--json]
```

### Sync (live/cache)

```bash
agenttrader sync --platform all --days 7 --limit 100
agenttrader sync --resolved --platform polymarket --days 365 --json
agenttrader sync --markets <id1> --markets <id2> --json
```

### Markets

```bash
agenttrader markets list --platform all --limit 100 --json
agenttrader markets price <market_id> --json
agenttrader markets history <market_id> --days 30 --json
agenttrader markets match --polymarket-slug "..." --json
agenttrader markets match --kalshi-ticker "..." --json
```

### Market screener

```bash
agenttrader markets screen --condition "current_price < 0.30" --json
agenttrader markets screen --condition "price_vs_7d_avg < -0.10" --platform polymarket --limit 20 --json
```

Supported condition metrics:

- `price_vs_7d_avg`
- `current_price`
- `volume`
- `days_until_close`
- `price_change_24h`

Supported operators: `<`, `>`, `<=`, `>=`, `==`.

### Validate

```bash
agenttrader validate ./strategy.py --json
```

### Backtests

Run:

```bash
agenttrader backtest ./strategy.py --from 2024-01-01 --to 2024-06-01 --cash 10000 --json
```

Optional guardrails:

```bash
agenttrader backtest ./strategy.py --from 2024-01-01 --to 2024-06-01 --max-markets 100 --fidelity bar_1h --json
```

- `--max-markets`: optional cap (default: no cap)
- `--fidelity`: `exact_trade` (default), `bar_1h`, or `bar_1d`

List runs:

```bash
agenttrader backtest list --json
```

Show one run:

```bash
agenttrader backtest show <run_id> --json
```

`backtest` returns summary metrics. Full heavy arrays are stored in artifacts and returned by `backtest show`.

Backtest output includes:

- `metrics`
- `resolution_accuracy`
- `by_category`
- `data_source`, `fidelity`, `max_markets_applied`, `markets_tested`

### Paper trading

```bash
agenttrader paper start ./strategy.py --cash 10000 --json
agenttrader paper status <portfolio_id> --json
agenttrader paper list --json
agenttrader paper stop <portfolio_id> --json
agenttrader paper stop --all --json
```

Compare parallel strategies:

```bash
agenttrader paper compare <portfolio_id_1> <portfolio_id_2> --json
agenttrader paper compare --all --json
```

Strategies hot-reload on file changes; `reload_count` tracks reloads.

### Experiments

```bash
agenttrader experiments log <backtest_run_id> --note "baseline" --tags "politics,mean-reversion" --json
agenttrader experiments log --portfolio <portfolio_id> --note "live snapshot" --json
agenttrader experiments list --json
agenttrader experiments show <experiment_id> --json
agenttrader experiments note <experiment_id> "updated note" --json
agenttrader experiments compare <exp_id_1> <exp_id_2> --json
```

### Dashboard

```bash
agenttrader dashboard
agenttrader dashboard --port 9090
```

## Strategy Interface

Example:

```python
from agenttrader import BaseStrategy

class MyStrategy(BaseStrategy):
    def on_start(self):
        self.subscribe(platform="polymarket", category="politics")

    def on_market_data(self, market, price, orderbook):
        history = self.get_history(market.id, lookback_hours=48)
        if len(history) < 2:
            return
        avg = sum(p.yes_price for p in history) / len(history)
        if price < avg - 0.08 and self.get_position(market.id) is None:
            self.buy(market.id, contracts=20)
        elif price > avg + 0.05 and self.get_position(market.id):
            self.sell(market.id)

    def on_schedule(self, now, market):
        pass

    def on_resolution(self, market, outcome, pnl):
        self.log(f"Resolved: {outcome}, pnl={pnl:.2f}")
```

Available methods include:

- `subscribe`, `search_markets`
- `get_price`, `get_orderbook`, `get_history`
- `get_position`, `get_cash`, `get_portfolio_value`
- `buy`, `sell`
- `log`, `set_state`, `get_state`

## Storage Layout

```text
~/.agenttrader/
├── config.yaml
├── db.sqlite
├── data/                          # parquet dataset (if downloaded)
├── backtest_index.duckdb          # normalized index (if built)
├── backtest_artifacts/            # msgpack+gzip heavy run artifacts
├── experiments.json               # experiment memory
├── logs/
│   └── performance.jsonl          # CLI/MCP timing logs
└── orderbooks/
```

## Data Source Selection for Backtests

Backtests auto-select the fastest available source:

1. `normalized-index` (`backtest_index.duckdb`) if built
2. parquet dataset fallback
3. SQLite sync-cache fallback

## Troubleshooting

- `agenttrader: command not found`
  - Activate your environment or reinstall: `pip install agenttrader`
- `NotInitialized`
  - Run `agenttrader init`
- `DatasetNotFound` on index build
  - Run `agenttrader dataset download` first
- PMXT import/sidecar issues
  - Ensure `pip install pmxt` and `npm install -g pmxtjs`
- Empty backtests
  - Expand date range, sync more data, or build index from full dataset

## Development

```bash
git clone https://github.com/finnfujimura/agenttrader
cd agenttrader
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

## License

MIT
