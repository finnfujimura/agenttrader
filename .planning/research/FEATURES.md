# Backtest Rust Migration Feature Set

## Context Anchors
- Mission: materially improve broad-subscription/full-span backtest performance without changing outcomes.
- Constraint: incremental migration only; no full rewrite.
- Constraint: Python strategy authoring contract remains stable.
- Risk anchor: correctness-critical simulation with deterministic parity requirements.
- Concern anchor: existing strict-mode legacy inconsistency must be handled before trusting fallback paths.
- Concern anchor: concurrency fragility in live loops means migration must not introduce hidden race behavior.
- Concern anchor: data-layer duplication drift must not be repeated in Rust modules.

## Table Stakes (Must-Have)

### TS1: Mandatory parity gate framework for every migrated slice
- Description: run legacy and Rust paths on identical inputs and compare `markets_tested`, `final_value`, full `equity_curve`, and full `trades`.
- Complexity: Medium.
- Dependencies: stable fixture corpus, deterministic serialization, CI runner support, diff tooling for large trade streams.
- Why this is table stakes: performance wins are irrelevant if correctness parity is not continuously enforced.

### TS2: Feature flags plus explicit rollback controls per slice
- Description: each Rust slice is independently toggled at config/CLI/MCP boundaries with safe fallback to Python path.
- Complexity: Medium.
- Dependencies: config plumbing, CLI flag propagation, MCP option propagation, observability around active path selection.
- Why this is table stakes: supports staged rollout and fast disable during regressions.

### TS3: Streaming executor hot loop Rust kernel (`_run_streaming`)
- Description: migrate event merge/dispatch/schedule-check/snapshot-gate hot loop to Rust while preserving event ordering semantics.
- Complexity: Large.
- Dependencies: formal event ordering contract, deterministic time handling, stable boundary type schema, property tests for identical ordering.
- Why this is table stakes: identified highest-ROI CPU bottleneck in current architecture.

### TS4: DuckDB-to-runtime marshalling reduction layer
- Description: replace Python row/object churn (`PricePoint`/`Market` inflation loops) with a Rust-backed compact transfer path.
- Complexity: Medium-Large.
- Dependencies: adapter boundary in `index_provider`/`index_adapter`, memory layout decisions, compatibility tests across fidelity modes.
- Why this is table stakes: query execution is already native-fast; transfer and Python object churn are the bottleneck.

### TS5: Deterministic fill/simulation core acceleration
- Description: migrate fill math, slippage/synthetic execution logic, and position update primitives to Rust with bit-for-bit stable behavior where feasible.
- Complexity: Large.
- Dependencies: numeric policy (rounding/precision), execution-mode compatibility matrix, deterministic test vectors, audit logs for fills.
- Why this is table stakes: synthetic execution model is a top compute path and correctness-sensitive.

### TS6: Pre-migration strict-mode/fallback correctness hardening
- Description: resolve legacy strict-mode inconsistency before relying on fallback parity comparisons.
- Complexity: Medium.
- Dependencies: regression tests around `strict_price_only` and legacy fallback behavior, context creation parameter audit.
- Why this is table stakes: parity gate credibility depends on the baseline path being internally consistent.

### TS7: Performance gate suite tied to representative workloads
- Description: enforce benchmark gates on broad-subscription/full-span runs, not only toy subsets.
- Complexity: Medium.
- Dependencies: reproducible benchmark harness, fixed datasets, threshold policy, artifact retention for regressions.
- Why this is table stakes: prevents local microbench wins that do not move real user workloads.

## Differentiators (High-Leverage, Not Strictly Required for V1)

### D1: Hybrid kernel planner (auto-select Rust/Python subpaths)
- Description: runtime planner chooses Rust kernels when preconditions are met, with transparent reasons when falling back.
- Complexity: Medium-Large.
- Dependencies: capability registry, clear precondition checks, explainability hooks in logs.
- Differentiation value: maximizes acceleration coverage without forcing all-or-nothing migration.

### D2: Cross-language deterministic trace mode
- Description: optional trace IDs and checkpoint emissions from both Python and Rust paths for side-by-side replay.
- Complexity: Medium.
- Dependencies: shared trace schema, low-overhead instrumentation, log normalization tooling.
- Differentiation value: shortens time-to-root-cause for parity mismatches and rollout incidents.

### D3: Replayable event tape for mismatch triage
- Description: persist compact event tapes that can replay the exact execution window in either runtime.
- Complexity: Large.
- Dependencies: stable tape format, snapshot controls, deterministic seed/time controls.
- Differentiation value: makes correctness investigations surgical instead of rerunning full backtests.

### D4: Rust ETL normalization workers for index build path
- Description: move heavy transforms/normalization loops in index build to Rust while keeping DuckDB SQL ownership unchanged.
- Complexity: Medium.
- Dependencies: ETL boundary contract, migration-safe schema versioning, migration tests over large datasets.
- Differentiation value: accelerates data preparation and shortens experiment turnaround time.

### D5: Boundary minimization via vectorized batch API
- Description: replace chatty FFI calls with coarse-grained vectorized kernels and batched returns.
- Complexity: Medium.
- Dependencies: API redesign at Python/Rust boundary, compatibility shims for old callers.
- Differentiation value: reduces overhead and unlocks compounding performance gains across slices.

## Anti-Features (Explicitly Excluded)

### AF1: Full backtest engine rewrite in Rust
- Description: reject replacing end-to-end engine in one pass.
- Complexity note: avoids very large coordination and regression surface.
- Dependency note: would require complete strategy/runtime contract rewrite, which is out of scope.

### AF2: Early Rust migration of PMXT sync/network or CLI/MCP orchestration
- Description: reject spending early migration budget on network-bound or glue layers.
- Complexity note: medium engineering effort for low runtime ROI.
- Dependency note: competes with high-ROI compute path dependencies.

### AF3: Parallel execution changes that alter deterministic ordering
- Description: reject throughput optimizations that modify event/fill ordering semantics.
- Complexity note: hidden correctness complexity is large.
- Dependency note: would require redefining parity invariants and user trust model.

### AF4: Silent path switching without user-visible telemetry
- Description: reject automatic fallback/upgrade behavior that is not logged and attributable.
- Complexity note: low effort to avoid, high incident cost if ignored.
- Dependency note: requires path-selection telemetry as a non-optional dependency.

### AF5: Duplicate Rust data trees or split migration entrypoints
- Description: reject parallel module hierarchies that mirror current data-layer drift concerns.
- Complexity note: maintainability complexity compounds over time.
- Dependency note: requires single-owner module map and import lint checks.

### AF6: FFI surface that bypasses strategy trust boundaries
- Description: reject exposing dynamic execution hooks that make strategy code less auditable.
- Complexity note: security and incident complexity increase sharply.
- Dependency note: requires explicit trust-model documentation and constrained interface design.

## Incremental Slice Order (Recommended)
- Slice 1: TS1 + TS2 + TS6 (safety baseline first).
- Slice 2: TS3 minimal kernel (event merge and dispatch only) behind flag.
- Slice 3: TS4 marshalling compaction with parity/perf gates.
- Slice 4: TS5 deterministic fill primitives and synthetic model parity.
- Slice 5: D5 vectorized boundary API, then D2 trace mode.
- Slice 6: D4 ETL acceleration after runtime-path confidence is established.

## Decision Rule
- Promote a slice from experimental to default only when parity is clean, rollback is verified, and representative benchmark gain is sustained.
