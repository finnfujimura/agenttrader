# Technical Concerns

## Scope
- Repository scan focused on bugs, security, performance, and fragile architecture.
- Key files reviewed include `agenttrader/mcp/server.py`, `agenttrader/core/backtest_engine.py`, `agenttrader/core/context.py`, `agenttrader/core/paper_daemon.py`, `agenttrader/data/cache.py`, `agenttrader/data/orderbook_store.py`, `agenttrader/data/backtest_artifacts.py`, `agenttrader/cli/dataset.py`, `agenttrader/cli/validate.py`, and `agenttrader/config.py`.

## High Severity
- Legacy backtest mode appears internally inconsistent in strict mode: `_run_legacy` always passes `context.get_orderbook(...)` into strategy callbacks (`agenttrader/core/backtest_engine.py:579`), but `BacktestContext.get_orderbook` raises in `strict_price_only` (`agenttrader/core/context.py:175`). `_run_legacy` also does not pass `config.execution_mode` when creating `BacktestContext` (`agenttrader/core/backtest_engine.py:505`), so strict mode is the default. This can break fallback runs unexpectedly.
- Paper trading polling uses thread fan-out (`agenttrader/core/paper_daemon.py:320` and `agenttrader/core/paper_daemon.py:331`) while mutating shared dict/deque state in `LiveContext.refresh_market_live` (`agenttrader/core/context.py:906`, `agenttrader/core/context.py:931`, `agenttrader/core/context.py:958`) without explicit locking. This is a concurrency fragility risk under load.
- Strategy files are executed directly via `importlib` (`agenttrader/mcp/server.py:1664`, `agenttrader/core/paper_daemon.py:398`, `agenttrader/cli/backtest.py:221`). Current validation in `agenttrader/cli/validate.py` is static AST linting only and can be bypassed by runtime tricks. Treat strategy code as fully trusted local code; there is no sandbox boundary.
- Process termination trusts stored PIDs and then marks portfolios stopped even if terminate fails (`agenttrader/mcp/server.py:2250`, `agenttrader/mcp/server.py:2269`, `agenttrader/cli/paper.py:136`). PID reuse can target unrelated processes in edge cases.

## Security and Data Integrity
- Dataset download has no checksum or signature verification before extraction (`agenttrader/cli/dataset.py:17`, `agenttrader/cli/dataset.py:124`, `agenttrader/cli/dataset.py:187`). A tampered artifact from the source URL would not be detected.
- External tar extraction path uses system `tar` first (`agenttrader/cli/dataset.py:187`) with no explicit member path validation in that branch. Python fallback uses `filter="data"` (`agenttrader/cli/dataset.py:70`), but the primary path does not enforce equivalent constraints.
- Backtest artifact reads are unbounded full-file decompress/load (`agenttrader/data/backtest_artifacts.py:18`). Large or corrupt gzip/msgpack files can cause memory spikes or decode failures in read flows.

## Performance and Scalability
- Legacy backtest constructs a full in-memory `events` list (`agenttrader/core/backtest_engine.py:519`, `agenttrader/core/backtest_engine.py:552`) after loading up to 10,000 markets (`agenttrader/core/backtest_engine.py:469`). This creates large memory pressure for long ranges.
- Price/history access in backtest contexts repeatedly scans full lists with list comprehensions (`agenttrader/core/context.py:167`, `agenttrader/core/context.py:210`, `agenttrader/core/context.py:529`), which can become O(n^2)-like in strategy loops.
- Orderbook nearest/latest lookup scans all stored files and all snapshots each call (`agenttrader/data/orderbook_store.py:62`, `agenttrader/data/orderbook_store.py:77`). Live loops that frequently call orderbook APIs will degrade as history grows.
- Batch point writes still perform one UPSERT per row (`agenttrader/data/cache.py:351`), causing high write amplification for large sync windows.
- `IndexProvider.get_latest_price` fetches full history from epoch to `2**31` then takes the tail (`agenttrader/data/index_provider.py:44`). This is expensive for repeated per-market reads.

## Architecture and Maintainability Debt
- `agenttrader/mcp/server.py` is a 2,490-line file with a single 1,281-line `call_tool` function (`agenttrader/mcp/server.py:1450`). This increases regression risk and makes tool behavior hard to reason about and test in isolation.
- There are duplicate data-layer trees under both `agenttrader/data/` and `agenttrader/db/data/` with visible divergence (for example `agenttrader/data/pmxt_client.py` vs `agenttrader/db/data/pmxt_client.py`, and `agenttrader/data/parquet_adapter.py` vs `agenttrader/db/data/parquet_adapter.py`). This is a major drift hazard.
- Migrations exist in two locations (`alembic/` and `agenttrader/db/migrations/`), and the root Alembic env hardcodes `~/.agenttrader/db.sqlite` (`alembic/env.py:14`) while runtime init uses packaged migrations (`agenttrader/cli/config.py:83`). This split can create operational confusion and accidental migration against the wrong database.
- Exception handling frequently swallows root causes and continues (`agenttrader/mcp/server.py:81`, `agenttrader/mcp/server.py:356`, `agenttrader/core/backtest_engine.py:314`, `agenttrader/core/paper_daemon.py:299`, `agenttrader/perf_logging.py:67`). This improves resilience but hides state corruption and makes incidents harder to diagnose.
- Cached index adapter probing is one-shot (`agenttrader/mcp/server.py:69`). If index is unavailable at first check, capabilities may stay stale for process lifetime unless restarted.
- Encoding artifacts are present in source comments (`agenttrader/config.py:206`, `agenttrader/data/index_builder.py:109`), indicating inconsistent text encoding handling and reduced maintainability/readability.

## Suggested Prioritization
- P0: Fix strict-mode legacy backtest inconsistency and add regression tests around fallback execution (`agenttrader/core/backtest_engine.py`, `agenttrader/core/context.py`).
- P0: Add explicit strategy trust model in docs and optional sandbox process boundary for untrusted strategies (`agenttrader/cli/validate.py`, `agenttrader/mcp/server.py`, `agenttrader/core/paper_daemon.py`).
- P1: Remove or quarantine duplicate `agenttrader/db/data/*` tree and standardize migration entrypoint (`agenttrader/db/data/*`, `alembic/*`, `agenttrader/db/migrations/*`).
- P1: Reduce live-loop contention with thread-safe state updates or single-threaded polling core (`agenttrader/core/paper_daemon.py`, `agenttrader/core/context.py`).
- P2: Address major hot paths (legacy event materialization, per-call orderbook scans, row-wise UPSERT loop) for predictable scaling.
