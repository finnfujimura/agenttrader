# Phase 1: Parity, Safety, and Benchmark Foundations - Context

**Gathered:** 2026-03-04
**Status:** Ready for planning

<domain>
## Phase Boundary

Phase 1 establishes the correctness and operational safety foundation for incremental Rust acceleration.  
It delivers strict parity validation, rollback/feature-flag controls, and benchmark gate policy that all later performance-bearing slices depend on.

</domain>

<decisions>
## Implementation Decisions

### Parity Contract
- Parity is strict canonical equality, not tolerance-based matching.
- Parity checks must include `markets_tested`, `final_value`, full `equity_curve`, and full `trades`.
- Event/order differences count as parity failures even when value sets match.
- Pass/fail gate style is deterministic hash plus strict field-level diff.
- In parity-enabled runs, full artifact comparison runs every time.
- Default mismatch reporting is first mismatch location plus summary counts.
- Parity outputs must include machine-readable JSON plus concise human summary.
- Default run status is failure when parity fails.
- Artifact retention policy keeps all failure artifacts plus a bounded rolling window of passing artifacts.

### Rollout Controls
- Use per-slice feature flags (executor, marshalling, fill) plus one global kill switch to force legacy execution.
- Default posture for enabled slices is guarded shadow mode before any Rust-primary promotion.
- Automatic fallback to legacy triggers on any parity failure or Rust runtime error.
- Flag/control surfaces must be available in both CLI and MCP backtest entrypoints.

### Benchmark Gates
- Use a representative tiered benchmark suite (small/medium/large), including broad-subscription full-span and mixed execution-mode workloads.
- Benchmark protocol is pinned and reproducible: fixed environment, warmup, repeated runs, median-based comparison.
- Define explicit minimum speedup and run-variance guard thresholds per migration slice.
- Performance cannot pass when parity fails; parity is a hard prerequisite for benchmark acceptance.

### Claude's Discretion
- Exact threshold values for per-slice speedup and variance.
- Exact retention window size for passing parity artifacts.
- Flag naming/details and specific JSON schema fields, as long as the locked behaviors above are preserved.

</decisions>

<specifics>
## Specific Ideas

- Migration remains incremental; no full rewrite.
- Python strategy authoring remains unchanged.
- Highest-ROI performance slices come after this foundation: streaming executor, then marshalling boundary, then fill/simulation.
- Phase 1 is intentionally designed for low regression risk while enabling measurable wins in subsequent phases.

</specifics>

<code_context>
## Existing Code Insights

### Reusable Assets
- `agenttrader/cli/utils.py` (`emit_json`, `json_errors`) for consistent CLI parity/fallback output.
- `agenttrader/mcp/server.py` (`_error_payload`, run/get_backtest tool responses, progress persistence helpers) for MCP-facing guard/parity status payloads.
- `agenttrader/core/backtest_engine.py` (`run`, `_run_streaming`, `_run_legacy`, `BacktestConfig`) as primary parity integration boundary.
- `agenttrader/data/models.py` (`ExecutionMode`) for explicit mode contracts in parity and rollout behavior.
- `agenttrader/data/cache.py` + `backtest_runs.results_json` for storing status/progress and lightweight parity metadata linkage.

### Established Patterns
- Backtest interfaces already expose `execution_mode`, `fidelity`, and `include_curve` through CLI/MCP contracts.
- CLI and MCP prefer structured machine-readable payloads with explicit `ok/error/status` fields.
- Operational safety patterns already exist for status normalization and graceful failure handling in MCP/server flows.
- Deterministic mode semantics are explicit in strict vs synthetic execution mode boundaries.

### Integration Points
- Add parity comparator/gating in backtest execution flow invoked by both CLI and MCP pathways.
- Extend CLI backtest args and MCP tool schemas to include per-slice flags and global legacy override.
- Persist parity artifacts and references via existing artifact/cache pathways with deterministic identifiers.
- Surface active path + guard/fallback outcome in run result payloads and `get_backtest` status views.

</code_context>

<deferred>
## Deferred Ideas

- Runtime visibility detail expansion (exact operator dashboard fields and richer telemetry views) can be elaborated in a future phase if needed.
- Event-tape replay and deep trace tooling remain v2 scope.

</deferred>

---

*Phase: 01-parity-safety-and-benchmark-foundations*
*Context gathered: 2026-03-04*
