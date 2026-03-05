# Phased Rust Integration Architecture for Backtesting

## Scope
- Integrate Rust into the backtest runtime incrementally, without breaking existing Python strategy contracts.
- Keep deterministic behavior and strict correctness parity as the first release gate for every migrated slice.
- Ship every Rust slice behind explicit feature flags with fast rollback to the Python implementation.

## Current Architecture Anchors
- Orchestration entrypoint: `agenttrader/core/backtest_engine.py`.
- Strategy-facing execution contract: `agenttrader/core/context.py` and `agenttrader/core/base_strategy.py`.
- Fill logic: `agenttrader/core/fill_model.py` and `agenttrader/core/price_fill_model.py`.
- Data stream input: `agenttrader/data/index_adapter.py` and `agenttrader/data/parquet_adapter.py`.
- Source routing policy: `agenttrader/data/source_selector.py`.
- Runtime types: `agenttrader/data/models.py`.
- Front-door callers: `agenttrader/cli/backtest.py` and `agenttrader/mcp/server.py`.
- Existing strict behavior coverage: `tests/unit/test_strict_backtest.py`, `tests/unit/test_no_silent_synthetic.py`, `tests/unit/test_strict_integration.py`, `tests/unit/test_backtest_streaming.py`.

## Target Component Boundaries
### Python Control Plane (remains authoritative)
- Request parsing and user interfaces remain in `agenttrader/cli/backtest.py` and `agenttrader/mcp/server.py`.
- Run lifecycle, persistence, and artifact wiring remain in `agenttrader/core/backtest_engine.py` and `agenttrader/data/backtest_artifacts.py`.
- Strategy lifecycle callbacks stay Python-native in `agenttrader/core/base_strategy.py`.
- ExecutionContext behavior and error model remain in `agenttrader/core/context.py` and `agenttrader/errors.py`.

### Rust Compute Plane (new)
- New crate root: `rust/agenttrader_kernel/Cargo.toml`.
- FFI entrypoint and ABI-safe wrappers: `rust/agenttrader_kernel/src/lib.rs`.
- Event kernel module: deterministic timestamp merge, schedule tick generation, and event ordering.
- Fill kernel module: strict and synthetic fill math parity with `FillResult`-equivalent outputs.
- Metrics kernel module: optional later slice for heavy vector math currently in `_compute_metrics`.

### Python-Rust Bridge (new)
- Bridge package: `agenttrader/rust_bridge/__init__.py` and `agenttrader/rust_bridge/runtime.py`.
- Bridge responsibilities:
- Resolve flags and capabilities.
- Marshal Python objects into typed arrays / compact structs.
- Call Rust kernels with explicit execution mode and fidelity.
- Convert kernel outputs back into current Python result schema.

## Data Flow
### Baseline Python path (unchanged)
1. CLI/MCP builds `BacktestConfig` in `agenttrader/cli/backtest.py` or `agenttrader/mcp/server.py`.
2. `BacktestEngine.run` in `agenttrader/core/backtest_engine.py` selects streaming or legacy path.
3. Data is streamed from `BacktestIndexAdapter` in `agenttrader/data/index_adapter.py`.
4. Events update `StreamingBacktestContext` in `agenttrader/core/context.py`.
5. Results serialize via `agenttrader/data/backtest_artifacts.py`.

### Rust shadow path (parity mode)
1. Same input config enters `BacktestEngine.run`.
2. Python path executes normally and emits canonical baseline result.
3. Rust bridge replays identical inputs through Rust kernels.
4. Parity comparator checks strict fields and full artifacts.
5. If mismatch occurs, runtime returns Python result and emits mismatch artifact + diagnostics.

### Rust primary path (flagged)
1. Same input config enters `BacktestEngine.run`.
2. Rust kernels execute selected slices directly.
3. Optional sampled shadow checks continue in background CI/prod canary mode.
4. Any parity mismatch or runtime failure auto-falls back to Python path for that run.

## Strict Parity Architecture
- Create comparator module: `agenttrader/core/parity.py`.
- Canonical payload for comparison includes:
- `markets_tested`
- `execution_mode`
- `fidelity`
- `final_value`
- Full `equity_curve` from artifact payload
- Full `trades` from artifact payload
- Canonicalization rule: stable key ordering + deterministic float normalization policy.
- Diff artifacts stored through `agenttrader/data/backtest_artifacts.py` with separate parity suffix.
- CI parity suites:
- `tests/parity/test_rust_streaming_parity.py`
- `tests/parity/test_rust_fill_parity.py`
- Reuse existing strict behavior tests as non-regression anchors:
- `tests/unit/test_strict_backtest.py`
- `tests/unit/test_no_silent_synthetic.py`
- `tests/unit/test_backtest_streaming.py`

## Feature Flag Topology
- Global runtime selector: `AGENTTRADER_BT_RUNTIME=python|rust_shadow|rust_primary`.
- Slice flags:
- `AGENTTRADER_BT_RUST_EVENTS=0|1`
- `AGENTTRADER_BT_RUST_MARSHAL=0|1`
- `AGENTTRADER_BT_RUST_FILLS=0|1`
- `AGENTTRADER_BT_RUST_METRICS=0|1`
- Parity enforcement flags:
- `AGENTTRADER_BT_PARITY=off|sampled|always`
- `AGENTTRADER_BT_PARITY_FAIL_CLOSED=0|1`
- Central flag parsing should live in `agenttrader/config.py` with one helper consumed by `agenttrader/core/backtest_engine.py`, `agenttrader/cli/backtest.py`, and `agenttrader/mcp/server.py`.

## Phased Build Order
### Phase 0: Parity and flags foundation
- Add runtime flag model and comparator plumbing in Python only.
- Add canonical result hashing and parity artifact output.
- Exit gate: no behavior change with default flags; parity harness validates Python-vs-Python replay.

### Phase 1: Rust bridge skeleton
- Add crate, packaging hooks in `pyproject.toml`, and bridge loader in `agenttrader/rust_bridge/runtime.py`.
- Implement no-op kernel that round-trips minimal payload and returns deterministic metadata.
- Exit gate: build/install stability across dev and CI; zero runtime impact when flags are off.

### Phase 2: Event ordering kernel
- Move event merge and schedule tick generation from `_run_streaming` loop into Rust.
- Keep strategy callbacks and position state updates in Python.
- Exit gate: strict parity green on full `equity_curve` and `trades` across fixture matrix.

### Phase 3: Marshalling optimization kernel
- Replace per-point Python object churn with batched Rust-friendly buffers.
- Keep exact sequence semantics from Phase 2.
- Exit gate: strict parity maintained; measurable wall-clock reduction on broad-market runs.

### Phase 4: Fill and position math kernel
- Move strict fill and synthetic fill primitives into Rust.
- Keep mode semantics identical to `ExecutionMode` in `agenttrader/data/models.py`.
- Exit gate: strict-mode and synthetic-mode suites pass with no diff.

### Phase 5: Staged rollout
- Enable `rust_primary` for canary subsets, keep automatic fallback enabled.
- Promote default only after sustained parity-clean and benchmark wins.
- Exit gate: rollback drill proves one-flag disable returns full Python path immediately.

## Build and Release Notes
- Keep Python as source-of-truth behavior until Phase 5 promotion.
- Keep one owner for data contracts to avoid split-brain across `agenttrader/data/*` and Rust modules.
- Treat parity failures as release-blocking for any slice that is enabled in production.
