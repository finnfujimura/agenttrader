# Requirements: AgentTrader Backtest Performance & Scalability

**Defined:** 2026-03-04
**Core Value:** Backtests run substantially faster at scale with unchanged strategy outcomes and no correctness regressions.

## v1 Requirements

### Parity and Validation

- [x] **PARI-01**: Maintainer can run legacy and Rust-backed paths on identical inputs and receive a strict pass/fail parity result.
- [x] **PARI-02**: Parity checks verify `markets_tested` and `final_value` equality for every comparison run.
- [x] **PARI-03**: Parity checks verify full `equity_curve` equivalence using strict diff and deterministic hash output.
- [x] **PARI-04**: Parity checks verify full `trades` equivalence using strict diff and deterministic hash output.
- [x] **PARI-05**: On parity failure, system emits machine-readable diagnostics that identify first mismatch location and mismatched field.

### Rollout and Safety Controls

- [ ] **SAFE-01**: Operator can enable or disable each Rust migration slice independently via feature flags.
- [ ] **SAFE-02**: Operator can force legacy Python execution globally at runtime without code changes.
- [ ] **SAFE-03**: System automatically falls back to legacy path when Rust path errors or parity guard fails in guarded modes.
- [ ] **SAFE-04**: CLI and MCP surfaces expose active execution path/flags so runtime behavior is transparent.

### Streaming Event Executor

- [ ] **EXEC-01**: Rust-backed streaming executor preserves deterministic k-way merge event ordering semantics used by Python path.
- [ ] **EXEC-02**: Rust-backed executor preserves schedule check behavior and callback trigger timing.
- [ ] **EXEC-03**: Rust-backed executor preserves snapshot gating behavior and emitted snapshot sequence.
- [ ] **EXEC-04**: Representative large backtests on `_run_streaming` complete with measurable runtime improvement versus legacy baseline.

### DuckDB Marshalling Boundary

- [ ] **MARS-01**: Backtest runtime can consume batched market/history payloads from DuckDB without per-row Python object inflation in hot paths.
- [ ] **MARS-02**: Batched marshalling path preserves value semantics and ordering versus existing `PricePoint`/`Market` interpretation.
- [ ] **MARS-03**: Broad-subscription/full-span workloads show measurable runtime and CPU reduction from boundary compaction.

### Fill and Simulation Core

- [ ] **FILL-01**: Rust-backed strict price fill logic produces identical trade outcomes to legacy path on parity suites.
- [ ] **FILL-02**: Rust-backed synthetic execution model produces identical fill decisions and portfolio state transitions on parity suites.
- [ ] **FILL-03**: Numeric policy (rounding/precision) is explicit, tested, and stable across Python and Rust implementations.

### Benchmark and Acceptance Gates

- [ ] **BENC-01**: Project defines representative benchmark datasets for broad-subscription/full-span and mixed execution-mode runs.
- [ ] **BENC-02**: Each migration slice has explicit success thresholds (performance impact target and max tolerated parity failures).
- [ ] **BENC-03**: Release candidate for Rust-primary mode requires passing parity and benchmark gates on representative workloads.

## v2 Requirements

### Extended Tooling

- **TOOL-01**: Maintainer can replay canonical event tapes to isolate cross-language mismatch root causes.
- **TOOL-02**: System provides deep cross-language trace mode for deterministic debugging with manageable overhead.

### ETL Acceleration Expansion

- **ETLX-01**: Index build/normalization ETL uses Rust kernels for selected high-volume transforms with preserved output parity.
- **ETLX-02**: ETL acceleration includes parallel batch processing controls and benchmark gates for index build throughput.

### Adaptive Runtime Selection

- **HYBR-01**: Runtime can auto-select Rust or Python per slice based on input characteristics and guard conditions.

## Out of Scope

| Feature | Reason |
|---------|--------|
| Full system rewrite in Rust | Violates incremental adoption constraint and raises regression risk |
| Rewriting DuckDB SQL logic | DuckDB execution is already native-fast; ROI is lower than Python hot loops |
| Rust migration of PMXT sync networking path | Primarily network-bound, not CPU-bound |
| Rust migration of MCP/CLI orchestration glue | Low throughput ROI compared to compute kernels |
| Changing Python strategy authoring model | Constraint requires strategy authoring to remain Python-native |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| PARI-01 | Phase 1 | Complete |
| PARI-02 | Phase 1 | Complete |
| PARI-03 | Phase 1 | Complete |
| PARI-04 | Phase 1 | Complete |
| PARI-05 | Phase 1 | Complete |
| SAFE-01 | Phase 1 | Pending |
| SAFE-02 | Phase 1 | Pending |
| SAFE-03 | Phase 5 | Pending |
| SAFE-04 | Phase 1 | Pending |
| EXEC-01 | Phase 2 | Pending |
| EXEC-02 | Phase 2 | Pending |
| EXEC-03 | Phase 2 | Pending |
| EXEC-04 | Phase 2 | Pending |
| MARS-01 | Phase 3 | Pending |
| MARS-02 | Phase 3 | Pending |
| MARS-03 | Phase 3 | Pending |
| FILL-01 | Phase 4 | Pending |
| FILL-02 | Phase 4 | Pending |
| FILL-03 | Phase 4 | Pending |
| BENC-01 | Phase 1 | Pending |
| BENC-02 | Phase 1 | Pending |
| BENC-03 | Phase 5 | Pending |

**Coverage:**
- v1 requirements: 22 total
- Mapped to phases: 22
- Unmapped: 0

---
*Requirements defined: 2026-03-04*
*Last updated: 2026-03-05 after roadmap mapping*
