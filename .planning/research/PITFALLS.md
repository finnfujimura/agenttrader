# Rust Migration Pitfalls for Deterministic Simulation (AgentTrader)

## Scope Fit
- This document targets incremental Rust adoption for `agenttrader` backtesting, not full rewrite.
- It is aligned to `.planning/PROJECT.md` constraints: exact correctness parity, Python strategy API stability, and mandatory feature-flag rollback.
- Suggested phase placement below assumes this migration sequence:
  1. Phase 0 - Baseline parity harness and observability hardening.
  2. Phase 1 - `_run_streaming` event loop slice behind flag.
  3. Phase 2 - DuckDB-to-runtime marshalling slice.
  4. Phase 3 - fill/simulation core slice.
  5. Phase 4 - index ETL acceleration slice.
  6. Phase 5 - staged rollout and default-on transition.

## Pitfalls

### 1) Event Ordering Drift In `_run_streaming`
- Warning signs:
  - Same `markets_tested` and trade count, but first divergence appears at a specific event timestamp.
  - Final `final_value` differs only on long ranges with many simultaneous timestamps.
  - Replays show `on_schedule` or `on_resolution` firing in a different relative order.
- Prevention strategy:
  - Make event tie-break rules explicit and identical between Python and Rust (timestamp, event type, market id, stable index).
  - Build golden tests around dense same-second event clusters.
  - Expose optional per-event trace diff mode for failing parity runs.
- Suggested phase placement:
  - Phase 0 (define canonical ordering contract), then enforce during Phase 1 implementation.

### 2) Timestamp And Timezone Normalization Mismatch
- Warning signs:
  - Drift occurs around midnight UTC boundaries or daylight-saving transitions.
  - Schedule callbacks fire one tick early or late on identical input data.
  - Equality checks fail due to microsecond truncation differences.
- Prevention strategy:
  - Normalize all engine boundaries to UTC epoch integers before FFI crossing.
  - Ban implicit local-time conversion in Rust code paths.
  - Add parity fixtures that target boundary timestamps and close/resolution edges.
- Suggested phase placement:
  - Phase 0 policy and fixtures, validated again in Phase 1 and Phase 3.

### 3) Floating-Point Behavior Divergence In PnL/Fills
- Warning signs:
  - Small per-trade rounding differences that accumulate into equity curve divergence.
  - Divergence appears only in synthetic execution mode.
  - Parity fails with identical trade count but different cash/position values.
- Prevention strategy:
  - Define numeric contract per calculation: decimal scaling or exact rounding points at boundaries.
  - Keep cross-language reference vectors for fee, slippage, and fill calculations.
  - Compare full `equity_curve` and full `trades`, not only aggregates.
- Suggested phase placement:
  - Phase 0 contract; Phase 3 is highest-risk execution point.

### 4) Execution Mode Semantics Drift (`strict_price_only` vs synthetic)
- Warning signs:
  - Rust path passes parity in one mode but fails in another.
  - Behavior silently falls back to synthetic-like behavior in strict mode.
  - Legacy fallback path errors differ from streaming path.
- Prevention strategy:
  - Treat execution mode as required explicit input across FFI boundary.
  - Port mode-specific test matrix from current unit tests before optimization.
  - Block promotion unless all supported mode permutations pass parity.
- Suggested phase placement:
  - Phase 0 matrix definition, then Phase 1 and Phase 3 gate criteria.

### 5) Python Context Contract Drift (Strategy API Breakage)
- Warning signs:
  - Existing strategies require edits after Rust slice lands.
  - Hook call timing (`on_start`, `on_market_data`, `on_schedule`, `on_stop`) changes.
  - Context read methods return same data but at different lifecycle points.
- Prevention strategy:
  - Freeze strategy-facing contract in tests before moving core loops.
  - Add contract tests focused on callback ordering and context visibility.
  - Keep Python-facing objects/stubs stable and adapt internally at boundary.
- Suggested phase placement:
  - Phase 0 contract freeze; ongoing hard gate in Phases 1-4.

### 6) FFI Ownership/Lifetime Bugs Creating Nondeterministic State
- Warning signs:
  - Rare crashes or inconsistent values under repeated identical runs.
  - Parity failures disappear when logging is enabled (timing-sensitive behavior).
  - Memory growth or stale snapshot data when iterating large ranges.
- Prevention strategy:
  - Keep first slices data-oriented and copy-based before introducing zero-copy optimizations.
  - Add soak tests: identical run repeated N times must hash-identical outputs.
  - Wrap boundary with strict schema/version checks and panic-to-error conversion.
- Suggested phase placement:
  - Phase 1 initial boundary design; optimize only after Phase 2 parity stability.

### 7) Marshalling Optimization Changes Data Semantics
- Warning signs:
  - Throughput improves but missing points appear in history-fed loops.
  - Market data ordering differs after bulk transfer refactor.
  - Object churn drops, but `on_market_data` counts differ from baseline.
- Prevention strategy:
  - First optimize representation, not selection semantics.
  - Preserve source selector output invariants (`index -> parquet -> cache`) before marshalling changes.
  - Validate point counts and sequence hashes per market before and after FFI transfer.
- Suggested phase placement:
  - Phase 2 primary risk item.

### 8) Incomplete Parity Gates (Metric-Only Validation)
- Warning signs:
  - `final_value` matches but trade chronology differs.
  - Regressions discovered only in downstream analysis tools.
  - Rollout confidence depends on spot checks instead of automated diff artifacts.
- Prevention strategy:
  - Enforce required gates from PROJECT: `markets_tested`, `final_value`, full `equity_curve`, full `trades`.
  - Store machine-readable diff reports for every Rust-enabled CI run.
  - Fail closed on missing artifacts or truncated comparison output.
- Suggested phase placement:
  - Must be completed in Phase 0 before any Rust path is merged.

### 9) Premature Parallelization Breaks Determinism
- Warning signs:
  - Same seed/input yields different outputs across repeated runs.
  - Divergence appears only on multicore hosts or CI runners.
  - Bugs vanish when forcing single-thread mode.
- Prevention strategy:
  - Default Rust slices to deterministic single-thread execution.
  - Introduce parallelism only with deterministic partition/merge proof and dedicated tests.
  - Add deterministic replay mode that logs execution partition decisions.
- Suggested phase placement:
  - Avoid in Phases 1-2; consider only late Phase 5 after parity confidence is high.

### 10) Feature Flags Too Coarse To Roll Back Safely
- Warning signs:
  - One flag controls multiple migrated behaviors across independent risk areas.
  - Incident rollback disables unrelated improvements.
  - MCP and CLI select different defaults for same run type.
- Prevention strategy:
  - Create one flag per migration slice (streaming loop, marshalling, fill math, ETL).
  - Keep flag plumbing centralized so CLI and MCP behavior is identical.
  - Add startup diagnostics that print active acceleration flags into run metadata.
- Suggested phase placement:
  - Phase 0 flag design, then mandatory in each phase before implementation lands.

### 11) Migrating Wrong Surfaces (Low-ROI Paths)
- Warning signs:
  - Engineering time spent on MCP/CLI wrappers or network sync path without material speedup.
  - Benchmarks improve micro-paths but full backtest wall-clock barely changes.
  - Team pressure grows for broad rewrite due sunk-cost momentum.
- Prevention strategy:
  - Keep ROI ranking from PROJECT as hard scope guard: streaming loop, marshalling, fill core, ETL.
  - Require benchmark delta against representative broad-subscription/full-span workloads.
  - Explicitly reject PRs that touch out-of-scope surfaces unless risk-reduction only.
- Suggested phase placement:
  - Governance in Phase 0 and enforced through all phases.

### 12) Data-Layer Split-Brain During Migration
- Warning signs:
  - Rust code binds to one data tree while Python runtime uses another (`agenttrader/data` vs `agenttrader/db/data`).
  - Migration behavior differs by import path or entry point.
  - Fixes in one module do not affect the actual runtime path.
- Prevention strategy:
  - Declare one canonical integration target path before Rust module wiring.
  - Add import-path assertions in tests to catch accidental use of duplicate trees.
  - Document authoritative migration entrypoints to avoid dual-stack confusion.
- Suggested phase placement:
  - Resolve in Phase 0 before any production Rust boundary is added.

### 13) Benchmark-Only Wins Without Realistic Regression Coverage
- Warning signs:
  - Synthetic microbenchmarks report large speedups, but representative backtests do not.
  - CI shows green on performance jobs while parity jobs are sparse/flaky.
  - Rollout decisions rely on one dataset or short windows.
- Prevention strategy:
  - Pair every performance claim with parity evidence on long-range, many-market workloads.
  - Use deterministic fixture sets plus at least one large historical run profile.
  - Track both p50 and p95 runtime with matching correctness artifacts.
- Suggested phase placement:
  - Phase 0 benchmark protocol definition; repeated at each phase exit gate.

## Exit Criteria For This Pitfall Register
- Every phase plan should map tasks to these pitfalls and explicitly state which risks are retired.
- No slice should advance to default-on until its mapped pitfalls have passing automated gates.