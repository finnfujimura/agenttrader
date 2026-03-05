# Incremental Rust Acceleration Stack for Deterministic Backtests

## Fit To This Repository
- Keep Python strategy authoring unchanged (`agenttrader/core/base_strategy.py`).
- Keep current orchestration surfaces unchanged (`agenttrader/cli/backtest.py`, `agenttrader/mcp/server.py`).
- Preserve exact deterministic behavior from current streaming execution (`agenttrader/core/backtest_engine.py`).
- Preserve strict execution semantics and no-silent-synthetic guarantees (`agenttrader/core/context.py`, `tests/unit/test_no_silent_synthetic.py`).
- Keep DuckDB as query engine and avoid rewriting SQL-heavy index build logic (`agenttrader/data/index_adapter.py`, `agenttrader/data/index_builder.py`).
- Enforce parity gates required in `.planning/PROJECT.md`: `markets_tested`, `final_value`, full `equity_curve`, full `trades`.

## Current Hot Paths To Target First
- Event executor loop and heap merge in `_run_streaming` (`agenttrader/core/backtest_engine.py`).
- Repeated per-event Python object operations in `StreamingBacktestContext` (`agenttrader/core/context.py`).
- DuckDB row -> Python `PricePoint` churn in streaming methods (`agenttrader/data/index_adapter.py`).
- Fill simulation math (`agenttrader/core/fill_model.py`, `agenttrader/core/price_fill_model.py`).

## Recommended Stack (Primary)
| Layer | Choice | Why for this repo | Confidence |
|---|---|---|---|
| Python binding | `PyO3` | Clean direct binding to CPython, minimal glue, good mixed Python/Rust workflow | High |
| Build integration | `setuptools-rust` first | Current project already uses setuptools in `pyproject.toml`; minimal packaging disruption | High |
| Optional dev UX | `maturin develop` in rust subdir | Faster local iteration for native module without forcing full packaging change | Medium |
| ABI strategy | `abi3` targeting 3.12+ | Repo supports Python 3.12/3.13; reduces wheel matrix and release friction | High |
| Data interchange | Packed arrays / tuples at FFI boundary, not Python dataclass per row | Reduces `PricePoint` object churn from `index_adapter` streams | High |
| Determinism policy | Single-threaded core, explicit ordering keys | Matches current `(ts, market_id, iterator_key, point)` ordering behavior | High |

## Recommended Crate Layout
- `rust/agenttrader_accel/Cargo.toml` for one extension module.
- Module split:
- `stream_kernel.rs`: event merge + schedule/snapshot gating.
- `fill_kernel.rs`: deterministic buy/sell fill math equivalent to current `FillModel` and `PriceOnlyFillModel`.
- `marshal.rs`: bulk conversion helpers for history rows and trade/equity outputs.
- Keep Python orchestrator as control plane:
- Python still discovers subscriptions and loads markets in `BacktestEngine`.
- Rust receives pre-resolved market IDs/history batches and returns deterministic event outputs.

## Incremental Rollout Plan (Small Safe Slices)
1. Slice A: Rust marshal helpers only.
- Replace hottest conversion loops in `agenttrader/data/index_adapter.py` with optional native conversion path.
- No behavior changes; measure CPU + memory reduction.
- Confidence: High.

2. Slice B: Rust fill kernel only.
- Mirror `simulate_buy`, `simulate_sell`, and strict price-only fills from `agenttrader/core/fill_model.py` and `agenttrader/core/price_fill_model.py`.
- Keep `StreamingBacktestContext` ownership/state transitions in Python.
- Confidence: High.

3. Slice C: Rust streaming event executor.
- Move heap pop/push loop + schedule checks + snapshot gating from `_run_streaming` in `agenttrader/core/backtest_engine.py`.
- Keep strategy callbacks (`on_market_data`, `on_schedule`, `on_resolution`) invoked in Python to preserve strategy API.
- Confidence: Medium.

4. Slice D: ETL assist (optional, late).
- Only accelerate pre/post ETL transforms around `agenttrader/data/index_builder.py` (file scanning, normalization helpers).
- Do not replace core DuckDB SQL normalization pipeline.
- Confidence: Low-Medium.

## Feature Flags And Rollback
- Add runtime flag (env + config) for each slice, e.g.:
- `AGENTTRADER_RUST_MARSHAL=0/1`
- `AGENTTRADER_RUST_FILL=0/1`
- `AGENTTRADER_RUST_STREAM=0/1`
- On any native error, log once and fall back to Python path in-process.
- Persist active acceleration flags in backtest result metadata in `BacktestRun.results_json` path touched by `agenttrader/cli/backtest.py` and `agenttrader/mcp/server.py`.
- Confidence: High.

## Parity Gate Design (Mandatory Before Default-On)
- Use existing streaming tests as baseline (`tests/unit/test_backtest_streaming.py`, `tests/unit/test_backtest_progress.py`).
- Add native-vs-python comparator tests with identical inputs asserting exact equality on:
- `markets_tested`
- `final_value`
- full `equity_curve`
- full `trades`
- execution mode and warnings payload
- Keep strict-mode invariants from `tests/unit/test_strict_backtest.py` and `tests/unit/test_no_silent_synthetic.py`.
- Confidence: High.

## Practical Toolchain Choices
- Rust toolchain: stable channel pinned via `rust-toolchain.toml`.
- Python build deps: add `setuptools-rust` (and `maturin` in dev extras only).
- CI matrix: build wheels for Linux/macOS/Windows x86_64 first; pure-Python fallback remains supported.
- Benchmark harness: use existing performance logging hooks in `agenttrader/perf_logging.py` plus dedicated backtest benchmark script.
- Confidence: Medium-High.

## Explicit Avoid Guidance
- Avoid full rewrite of `BacktestEngine`/context orchestration in Rust; violates project scope in `.planning/PROJECT.md`.
- Avoid moving strategy callback execution into Rust; breaks Python strategy ecosystem and increases risk.
- Avoid introducing multithreaded/non-deterministic execution in early slices (no Rayon in hot loop initially).
- Avoid relying on hash-map iteration order for event processing; keep explicit sorted/heap order.
- Avoid changing default execution semantics (`strict_price_only`) or silent orderbook synthesis behavior.
- Avoid replacing DuckDB SQL normalization logic in `agenttrader/data/index_builder.py` during early acceleration phases.

## Recommended Decision
- Proceed with `PyO3 + setuptools-rust` as the default incremental stack, with optional `maturin develop` for local native iteration.
- Start with marshal + fill kernels before stream loop migration.
- Require parity and rollback gates before enabling any Rust slice by default.

