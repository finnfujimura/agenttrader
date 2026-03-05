# Roadmap: AgentTrader Backtest Performance & Scalability

## Overview

This roadmap delivers incremental Rust acceleration for the highest-ROI backtest compute paths while preserving exact deterministic correctness. The sequence is dependency-first: establish parity and safety gates, accelerate each hot slice behind flags, then promote only after release qualification gates pass.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: Parity, Safety, and Benchmark Foundations** - Establish strict parity validation, rollback controls, and benchmark gates that all later slices depend on.
- [ ] **Phase 2: Deterministic Streaming Executor Acceleration** - Replace `_run_streaming` hot-path execution with a Rust-backed path that preserves ordering and callback semantics.
- [ ] **Phase 3: DuckDB Marshalling Compaction** - Introduce batched boundary transfer to reduce Python object churn while preserving data semantics.
- [ ] **Phase 4: Fill and Simulation Kernel Parity** - Accelerate strict and synthetic fill logic with explicit numeric policy and parity guarantees.
- [ ] **Phase 5: Rust-Primary Release Qualification** - Enforce guarded promotion criteria and automatic fallback for Rust-primary release readiness.

## Phase Details

### Phase 1: Parity, Safety, and Benchmark Foundations
**Goal**: Maintainers can compare legacy and Rust-backed slices with strict deterministic parity, operate per-slice rollback controls, and run representative benchmark gates.
**Depends on**: Nothing (first phase)
**Requirements**: PARI-01, PARI-02, PARI-03, PARI-04, PARI-05, SAFE-01, SAFE-02, SAFE-04, BENC-01, BENC-02
**Success Criteria** (what must be TRUE):
  1. Maintainer can run legacy and Rust-backed paths on identical inputs and receive a strict pass/fail parity result.
  2. Parity output verifies equality for `markets_tested`, `final_value`, full `equity_curve`, and full `trades`, with deterministic diff/hash artifacts.
  3. On parity failure, machine-readable diagnostics identify first mismatch location and mismatched field.
  4. Operator can independently enable/disable migration slices, force global legacy execution at runtime, and see active path flags in CLI and MCP.
  5. Representative benchmark datasets and per-slice acceptance thresholds are defined and executable.
**Plans**: 1/3 complete (01 complete; 02-03 pending)

### Phase 2: Deterministic Streaming Executor Acceleration
**Goal**: The streaming event executor runs faster via Rust while preserving exact ordering, schedule checks, callbacks, and snapshot behavior.
**Depends on**: Phase 1
**Requirements**: EXEC-01, EXEC-02, EXEC-03, EXEC-04
**Success Criteria** (what must be TRUE):
  1. Rust-backed streaming execution preserves deterministic k-way merge event ordering versus legacy Python.
  2. Schedule checks and callback trigger timing match legacy behavior on parity suites.
  3. Snapshot gating behavior and emitted snapshot sequence are equivalent to legacy output.
  4. Representative large `_run_streaming` workloads show measurable runtime improvement while parity checks remain passing.
**Plans**: TBD

### Phase 3: DuckDB Marshalling Compaction
**Goal**: Backtest runtime consumes compact batched DuckDB payloads to reduce boundary overhead without semantic drift.
**Depends on**: Phase 2
**Requirements**: MARS-01, MARS-02, MARS-03
**Success Criteria** (what must be TRUE):
  1. Hot-path consumption uses batched market/history payloads without per-row Python object inflation.
  2. Batched marshalling preserves value semantics and ordering relative to existing `PricePoint`/`Market` interpretation.
  3. Broad-subscription and full-span workloads show measurable runtime and CPU reductions from boundary compaction.
**Plans**: TBD

### Phase 4: Fill and Simulation Kernel Parity
**Goal**: Fill and simulation logic is accelerated in Rust with exact deterministic outcomes across strict and synthetic execution modes.
**Depends on**: Phase 3
**Requirements**: FILL-01, FILL-02, FILL-03
**Success Criteria** (what must be TRUE):
  1. Rust-backed strict price fill logic produces trade outcomes identical to the legacy path on parity suites.
  2. Rust-backed synthetic execution model produces fill decisions and portfolio state transitions identical to the legacy path on parity suites.
  3. Numeric rounding and precision policy is explicit and proven stable across Python and Rust implementations.
**Plans**: TBD

### Phase 5: Rust-Primary Release Qualification
**Goal**: Rust-primary mode is promotable only when guarded execution, automatic fallback, and release acceptance gates are demonstrably satisfied.
**Depends on**: Phase 4
**Requirements**: SAFE-03, BENC-03
**Success Criteria** (what must be TRUE):
  1. In guarded modes, runtime automatically falls back to legacy execution when a Rust-path error or parity-guard failure occurs.
  2. Release-candidate runs for Rust-primary mode are promotable only after representative parity and benchmark gates pass.
  3. Operator can execute a guard/fallback drill that confirms rollback behavior without code changes.
**Plans**: TBD

## Progress

**Execution Order:**
Phases execute in numeric order: 1 -> 2 -> 3 -> 4 -> 5

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Parity, Safety, and Benchmark Foundations | 1/3 | In Progress | - |
| 2. Deterministic Streaming Executor Acceleration | 0/TBD | Not started | - |
| 3. DuckDB Marshalling Compaction | 0/TBD | Not started | - |
| 4. Fill and Simulation Kernel Parity | 0/TBD | Not started | - |
| 5. Rust-Primary Release Qualification | 0/TBD | Not started | - |
