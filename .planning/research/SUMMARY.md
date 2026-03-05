# Project Research Summary

**Project:** AgentTrader Backtest Performance & Scalability
**Domain:** Deterministic backtesting engine acceleration (Python control plane with Rust compute kernels)
**Researched:** 2026-03-05
**Confidence:** MEDIUM

## Executive Summary

This project is a correctness-critical performance initiative for a deterministic backtesting engine, not a net-new product build. The research is aligned: experts recommend keeping the Python strategy and orchestration contract stable while moving only the hottest compute kernels into Rust in small, reversible slices.

The recommended approach is parity-first incremental migration. Start with parity harnesses, strict mode hardening, and per-slice rollback flags, then migrate in order: streaming event executor, DuckDB-to-runtime marshalling, and fill/simulation math. Keep DuckDB SQL ownership in Python, keep strategy callbacks in Python, and use PyO3 with setuptools-rust plus abi3 targeting Python 3.12+ to minimize packaging risk.

The main risks are deterministic drift (event ordering, timestamps, and fill rounding), semantic drift between strict and synthetic execution modes, and rollout risk from coarse flags or incomplete parity checks. Mitigation is clear: enforce full artifact parity gates (`markets_tested`, `final_value`, full `equity_curve`, full `trades`), keep single-thread deterministic kernels initially, and require explicit diagnostics and fast fallback to Python on any mismatch.

## Key Findings

### Recommended Stack

The stack recommendation is consistent and implementation-ready: PyO3 bindings, setuptools-rust packaging, and optional maturin for faster local native iteration. The migration should preserve current Python entrypoints and use compact batch-oriented FFI payloads instead of per-row object conversion.

**Core technologies:**
- `PyO3`: Python-Rust extension bindings with low glue overhead and a mature ecosystem.
- `setuptools-rust`: lowest-disruption build integration with the current setuptools-based project.
- `abi3` (Python 3.12+): reduces wheel matrix complexity while matching repo Python support.
- Rust stable toolchain (pinned): reproducible builds across CI and local environments.
- DuckDB retained as query engine: avoids rewriting already-native-fast SQL-heavy paths.
- Batched boundary payloads: lowers Python object churn and cross-language call overhead.
- Single-thread deterministic kernels first: protects ordering guarantees and parity confidence.

### Expected Features

Research converges on safety and observability as table stakes before any default-on acceleration.

**Must have (table stakes):**
- Mandatory parity gate framework for every migrated slice.
- Per-slice feature flags with explicit rollback controls across CLI and MCP.
- Streaming hot-loop kernel migration with preserved ordering semantics.
- DuckDB-to-runtime marshalling reduction path.
- Deterministic fill/simulation acceleration across execution modes.
- Strict mode and fallback correctness hardening before trust in comparisons.
- Representative workload benchmark gates, not microbench-only validation.

**Should have (competitive):**
- Hybrid runtime planner that selects Rust/Python subpaths based on preconditions.
- Cross-language deterministic trace mode for fast parity mismatch debugging.
- Vectorized batch API to minimize FFI chattiness and compound performance gains.

**Defer (v2+):**
- Replayable event tape system for surgical mismatch triage.
- ETL acceleration for index build/normalization after runtime-path confidence is established.
- Optional heavy metrics-kernel offload after core runtime parity is stable.

### Architecture Approach

Architecture should remain split into a Python control plane and a Rust compute plane, connected by a thin bridge that owns flags, marshalling, and deterministic boundary contracts. Rollout should proceed from Python parity/shadow mode to flagged Rust primary mode, with automatic fallback on mismatch or runtime failure.

**Major components:**
1. Python control plane (`core`, `cli`, `mcp`) - run lifecycle, strategy callbacks, persistence, and user-facing behavior.
2. Rust compute plane (`event`, `marshal`, `fill`, optional `metrics` kernels) - CPU-heavy deterministic kernels only.
3. Python-Rust bridge (`rust_bridge`) - capability checks, config flags, type conversion, and kernel dispatch.
4. Parity/comparator + artifact pipeline - canonical comparisons and machine-readable diff artifacts.

### Critical Pitfalls

1. **Event ordering drift** - define explicit tie-break ordering contract and test dense same-timestamp clusters.
2. **Timestamp/timezone mismatch** - normalize to UTC epoch integers at boundaries and test DST/midnight edges.
3. **Floating-point and fill drift** - lock rounding policy and validate full trade/equity artifacts, not aggregates only.
4. **Execution mode semantic drift** - pass mode explicitly across FFI and gate every mode permutation.
5. **Incomplete parity/rollback controls** - require full artifact gates and fine-grained flags with startup diagnostics.

## Implications for Roadmap

Based on research, suggested phase structure:

### Phase 1: Parity and Rollback Foundation
**Rationale:** Every later slice depends on trustworthy correctness gates and fast disable paths.
**Delivers:** Comparator module, canonical artifact hashing/diff output, strict-mode hardening, centralized runtime/slice flag parsing.
**Addresses:** TS1, TS2, TS6.
**Avoids:** Incomplete parity gates, coarse rollback flags, strict-vs-synthetic mode ambiguity.

### Phase 2: Rust Bridge and Build Skeleton
**Rationale:** Establish stable packaging/loading before moving any behavior-critical logic.
**Delivers:** Rust crate scaffold, PyO3 extension wiring, setuptools-rust integration, no-op kernel path, CI build matrix smoke coverage.
**Addresses:** Stack adoption prerequisites and rollout safety.
**Uses:** `PyO3`, `setuptools-rust`, `abi3`.
**Implements:** Python-Rust bridge boundary.

### Phase 3: Deterministic Event Kernel
**Rationale:** Streaming loop is the highest-ROI bottleneck and should be first performance-bearing kernel.
**Delivers:** Flagged Rust event merge/schedule kernel with strategy callbacks remaining Python-side.
**Addresses:** TS3.
**Implements:** Rust compute plane event module.
**Avoids:** Event ordering drift, timezone normalization drift, callback contract breakage.

### Phase 4: Marshalling Compaction
**Rationale:** After event kernel stability, remove major Python object churn at data boundary.
**Delivers:** Batched compact transfer path replacing row-by-row `PricePoint` inflation in hot paths.
**Addresses:** TS4, D5.
**Implements:** Bridge marshalling layer.
**Avoids:** Data semantic drift during optimization and split-brain data tree behavior.

### Phase 5: Fill and Simulation Kernel
**Rationale:** Fill math is high impact and correctness-sensitive, so it follows established parity infrastructure.
**Delivers:** Rust strict and synthetic fill primitives with full matrix parity suites.
**Addresses:** TS5.
**Implements:** Rust fill kernel module.
**Avoids:** Floating-point accumulation drift and execution mode semantic mismatch.

### Phase 6: Canary Rollout and Observability
**Rationale:** Default-on promotion should happen only after sustained parity-clean and benchmark improvements.
**Delivers:** `rust_primary` canary rollout, sampled/always parity policies, fallback drills, benchmark SLO gates, trace instrumentation.
**Addresses:** TS7, D2 (and optional D1 rollout logic).
**Avoids:** Low-ROI migration drift, benchmark-only wins, and incident-unsafe promotion.

### Phase 7: ETL Acceleration (Optional V2)
**Rationale:** Valuable, but lower ROI than runtime kernels and should wait for runtime confidence.
**Delivers:** Rust-assisted ETL transforms around index build boundaries without replacing DuckDB SQL ownership.
**Addresses:** D4.
**Avoids:** Scope creep into low-confidence or low-impact surfaces before core migration is stable.

### Phase Ordering Rationale

- The sequence is dependency-driven: parity + flags first, then boundary scaffolding, then behavior-critical kernels.
- It aligns with architecture boundaries: bridge foundation before compute-kernel substitution.
- It retires highest-risk pitfalls earliest (ordering, mode semantics, rollback safety) before default-on rollout.

### Research Flags

Phases likely needing deeper research during planning:
- **Phase 3:** deterministic ordering contract details and cross-language event semantics.
- **Phase 5:** numeric policy and exact rounding/precision contract for strict and synthetic fills.
- **Phase 7:** ETL ROI validation and boundary selection against current DuckDB pipeline.

Phases with standard patterns (skip research-phase):
- **Phase 1:** parity harness + flags are well-defined from existing tests and project constraints.
- **Phase 2:** PyO3 bridge scaffolding and packaging are established patterns.
- **Phase 6:** canary/rollback/observability rollout follows common staged-release patterns.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | Converged recommendations with concrete tooling choices tied to current repo constraints. |
| Features | HIGH | Clear table-stakes vs differentiators with explicit anti-features and slice order guidance. |
| Architecture | MEDIUM | Strong boundary design, but kernel contracts still need implementation-time validation. |
| Pitfalls | HIGH | Detailed, repo-specific risk register with warning signs and phase-mapped prevention steps. |

**Overall confidence:** MEDIUM

### Gaps to Address

- Numeric contract gap: exact float rounding and normalization policy must be fixed before Phase 5 implementation.
- Data-contract gap: canonical ownership between potentially duplicated data trees must be asserted in tests.
- Benchmark policy gap: representative datasets and promotion thresholds need explicit SLO definitions.
- Packaging gap: full wheel/build stability across target OS matrix is assumed but not yet validated in this cycle.
- Rollout telemetry gap: parity diff artifact schema and retention policy should be finalized before canary expansion.

## Sources

### Primary (HIGH confidence)
- `.planning/PROJECT.md` - project scope, constraints, and correctness gates.
- `.planning/research/STACK.md` - recommended technology stack and rollout slices.
- `.planning/research/FEATURES.md` - table stakes, differentiators, anti-features, and slice priority.
- `.planning/research/ARCHITECTURE.md` - control/compute boundary model and phased integration path.
- `.planning/research/PITFALLS.md` - risk register, warning signals, and prevention guidance.

### Secondary (MEDIUM confidence)
- None provided in upstream research outputs.

### Tertiary (LOW confidence)
- None provided in upstream research outputs.

---
*Research completed: 2026-03-05*
*Ready for roadmap: yes*
