# Phase 1 Research: Parity, Safety, and Benchmark Foundations

Date: 2026-03-05
Phase: 01-parity-safety-and-benchmark-foundations
Status: Research complete, ready for planning

## Goal Alignment

Phase 1 must establish a deterministic gate before any Rust-primary slice work:
- strict parity contract for run outputs
- operator safety controls (flags and rollback)
- benchmark protocol and acceptance policy

This directly covers:
- PARI-01..PARI-05
- SAFE-01, SAFE-02, SAFE-04
- BENC-01, BENC-02

SAFE-03 and BENC-03 are phase-mapped later, but design decisions in Phase 1 should not block them.

## Codebase Findings (Current State)

### Backtest flow and control points

Current backtest entrypoints:
- CLI: `agenttrader/cli/backtest.py` (`_backtest_run`)
- MCP: `agenttrader/mcp/server.py` (`run_backtest`, `validate_and_backtest`)

Both converge on:
- `agenttrader/core/backtest_engine.py`
- `BacktestEngine.run()`

Backtest engine runtime behavior today:
- tries `BacktestIndexAdapter` streaming path first
- falls back to legacy path only for specific index errors:
  - `NoDataInRange`
  - `NoSubscriptions`
  - `DatasetNotFound`
- fallback metadata is already attached:
  - `fallback_from = "normalized-index"`
  - `fallback_reason = <message>`

Execution internals:
- `_run_streaming` supports `fidelity`:
  - `exact_trade`
  - `bar_1h`
  - `bar_1d`
- `_run_streaming` supports `execution_mode` and writes it to result payload
- `_run_legacy` currently always returns `fidelity = "exact_trade"` but also returns `execution_mode` field

Progress instrumentation already exists:
- preflight event payload
- periodic progress event payload
- consistent fields include:
  - `markets_tested`
  - `max_markets_applied`
  - `processed_units`
  - `percent_complete`
  - `throughput_per_second`
  - `eta_seconds`

### CLI and MCP surfaces (parity/safety impact)

CLI backtest surface today:
- supports `--from`, `--to`, `--cash`, `--max-markets`, `--fidelity`, `--json`
- does not expose `execution_mode`
- does not expose any parity/slice flags yet

MCP backtest surface today:
- `run_backtest` supports `fidelity`, `execution_mode`, `include_curve`
- `validate_and_backtest` supports same
- `get_backtest` can include artifacts on demand (`include_curve`)
- `list_backtests` returns progress if run is still running

Operational implication:
- SAFE-04 is partially satisfied in MCP but not CLI for execution control transparency
- Phase 1 should close CLI/MCP parity on flags and active-path reporting

### Result artifacts and persistence

Storage model is split:
- lightweight run metadata and status in SQLite (`backtest_runs.results_json`)
- full `equity_curve` and `trades` in compressed artifact:
  - `ARTIFACTS_DIR/<run_id>.msgpack.gz`
  - helper: `agenttrader/data/backtest_artifacts.py`

Current finalization pattern:
- run inserted with `status="running"`
- progress callback mutates `results_json` with preflight/progress
- on success:
  - summary result persisted to `results_json`
  - full arrays written to artifact file
- on failure:
  - `status="failed"`
  - traceback/error saved

Important persistence nuance:
- `BacktestRun` schema includes `execution_mode` column
- CLI and MCP run creation currently do not explicitly set that column
- column default is `strict_price_only`, so persisted row can diverge from actual run mode
- actual mode is still present in final result payload

### Execution mode semantics (current contracts)

Enum contract:
- `strict_price_only`
- `observed_orderbook`
- `synthetic_execution_model`

Context behavior:
- strict mode:
  - orderbook access raises `NoObservedOrderbook`
  - fills use observed price model (no synthetic orderbook)
- observed orderbook mode:
  - backtest contexts require observed orderbook history; otherwise error
  - streaming context currently errors for observed mode (historical orderbook not wired there)
- synthetic mode:
  - synthetic orderbook generated when observed data unavailable

Planning implication:
- parity matrix must include execution mode because trade outcomes differ by mode
- observed mode in streaming path is presently a deliberate failure mode unless data support is added

## Existing Validation and Gaps

What is already validated:
- fallback to legacy when index range/subscription coverage is missing
- artifact read/write roundtrip
- streaming progress emission
- strict mode behavior and "no silent synthetic" guarantees
- MCP list-backtests progress extraction

What is missing for Phase 1:
- no strict legacy-vs-rust parity comparator
- no deterministic hash/diff artifact standard
- no parity gate status in run lifecycle
- no feature-flag/kill-switch control layer for migration slices
- no benchmark suite runner with threshold gating
- no retention policy for parity artifacts (failures always retained + bounded pass window)

## Standard Stack

Use existing stack and avoid new heavy dependencies unless required:
- orchestration: existing CLI + MCP handlers
- persistence: existing SQLite `BacktestRun.results_json` plus artifact files
- comparison/hashing: Python stdlib (`json`, `hashlib`, `pathlib`) is sufficient
- serialization: reuse existing msgpack artifact channel for large arrays
- structured error payloads: reuse `emit_json`, `json_errors`, `_error_payload`

## Architecture Patterns

Pattern 1: Shared core, thin surface adapters
- keep parity/flag/gate logic inside core backtest service layer
- CLI and MCP should only map arguments and present output

Pattern 2: Dual artifact strategy
- summary and status in DB JSON
- large arrays in compressed artifact files
- parity diagnostics should follow same split to avoid DB bloat

Pattern 3: Explicit run state machine
- running -> complete/failed already exists
- add explicit parity status fields in payload for deterministic gating

Pattern 4: Deterministic machine-first diagnostics
- first mismatch location and counts
- strict path-style pointers for mismatches
- stable hashes for quick pass/fail comparisons

## Validation Architecture

Design objective:
- every parity-enabled run produces a reproducible comparison report between baseline and candidate execution paths

Recommended validation pipeline:
1. Execute baseline path and candidate path with identical normalized config.
2. Materialize canonical comparison payload per run:
   - `markets_tested`
   - `final_value`
   - `equity_curve`
   - `trades`
3. Canonicalize values to a deterministic JSON form:
   - sorted dict keys
   - stable list ordering as produced by engine
   - no tolerance-based rounding (strict equality)
4. Hash canonical payload with SHA-256 for quick checks.
5. If hash mismatch, run field-level diff:
   - first mismatch path
   - mismatch type
   - expected vs actual excerpts
   - total mismatch counts by top-level field
6. Persist diagnostics:
   - summary in `results_json` (small, queryable)
   - detailed diff artifact in artifact directory (large)
7. Set run gate outcome:
   - parity pass => run may continue to benchmark gate
   - parity fail => fail status for parity-enabled mode

Recommended result schema extension (minimum):
- `parity.enabled`
- `parity.status` (`pass` | `fail` | `skipped`)
- `parity.baseline_hash`
- `parity.candidate_hash`
- `parity.first_mismatch_path`
- `parity.mismatch_counts`
- `parity.artifact_path`

Recommended benchmark gate protocol:
- fixed strategy/date windows and fixed execution mode per case
- warmup runs excluded from scoring
- N measured runs, median runtime used
- store baseline medians under versioned benchmark profile
- require parity pass before speedup evaluation

## Don't Hand-Roll

Avoid custom frameworks for:
- feature flag storage service (keep local config/env plus existing config loader)
- generic diff engines beyond minimal deterministic comparator needs
- benchmark harness orchestration outside existing CLI/MCP execution flow

## Common Pitfalls

- Writing full equity/trades into `results_json` and causing SQLite bloat.
- Comparing non-canonical JSON and getting false mismatches from key ordering.
- Treating missing observed orderbooks as implicit synthetic behavior.
- Forgetting CLI support while adding MCP-only controls (violates SAFE-04 intent).
- Persisting default `execution_mode` in DB row while run actually used another mode.
- Benchmarking before parity pass and creating invalid performance claims.

## Code Examples (Where to Integrate)

Core parity integration target:
- `agenttrader/core/backtest_engine.py`
- add parity wrapper at/around `BacktestEngine.run()` boundary

CLI flag and reporting integration:
- `agenttrader/cli/backtest.py`
- extend argument parser and output payload/reporting

MCP flag and reporting integration:
- `agenttrader/mcp/server.py`
- extend `run_backtest` and `validate_and_backtest` schemas and handlers

Persistence touchpoints:
- `agenttrader/db/schema.py` (`BacktestRun` row fields)
- `agenttrader/data/backtest_artifacts.py` (detailed parity diff artifacts)
- `agenttrader/data/cache.py` access methods

## Planning-Ready Workstreams

Workstream A: Parity core
- canonicalization + hashing module
- strict diff module
- parity result schema and status integration

Workstream B: Safety controls
- per-slice flags + global legacy override
- unified config resolution for CLI and MCP
- active-path exposure in outputs

Workstream C: Persistence and artifact policy
- parity summary persistence in `results_json`
- detailed artifact write/read helpers
- retention policy implementation for pass/fail artifacts

Workstream D: Benchmark gate foundation
- benchmark case manifest format
- runner command and result persistence
- threshold evaluation with explicit fail reasons

Workstream E: Test harness
- unit tests for canonical parity compare
- contract tests for CLI/MCP flag propagation
- integration tests for run-state and artifact retention

## Open Questions to Resolve During Planning

- Which exact slice flag names should be standardized in CLI and MCP?
- Should parity run both paths in one invocation, or reuse cached baseline artifacts?
- Where should benchmark baselines be versioned (repo file vs runtime state dir)?
- Should parity diagnostics be viewable in dashboard APIs in Phase 1 or deferred?
- Do we require execution_mode persistence in DB row to match results payload for all runs?

## Suggested Phase 1 Exit Criteria

- Deterministic strict parity check implemented and wired into CLI + MCP.
- Parity failures produce machine-readable first-mismatch diagnostics.
- Runtime controls include per-slice enable flags and global legacy override.
- Run outputs explicitly state active execution path/flags in both CLI and MCP.
- Benchmark protocol and thresholds are codified and executable.
- Tests cover parity pass/fail, fallback behavior, and artifact/status persistence.
