# AgentTrader Backtest Performance & Scalability

## What This Is

This project incrementally improves `agenttrader` backtest runtime performance and scalability for large broad-subscription/full-span runs while preserving exact correctness. The focus is targeted native acceleration in the highest-ROI compute paths, with strict parity gates and rollback safety. The audience is internal maintainers and users running large-scale research backtests through CLI and MCP flows.

## Core Value

Backtests run substantially faster at scale with unchanged strategy outcomes and no correctness regressions.

## Requirements

### Validated

- ✓ Deterministic backtest execution exists across multiple fidelity and execution modes (`agenttrader/core/backtest_engine.py`, `agenttrader/core/context.py`) — existing.
- ✓ DuckDB-backed indexed history path exists and is already native-fast for query execution (`agenttrader/data/index_adapter.py`, `agenttrader/data/index_provider.py`) — existing.
- ✓ CLI and MCP backtest surfaces exist and are production-critical (`agenttrader/cli/backtest.py`, `agenttrader/mcp/server.py`) — existing.
- ✓ Strategy authoring/runtime contract is Python-based and shared across execution contexts (`agenttrader/core/base_strategy.py`) — existing.

### Active

- [ ] Deliver a phased Rust migration plan for highest-ROI targets with smallest safe slices first.
- [ ] Accelerate streaming backtest hot loop (`_run_streaming`) while preserving exact behavior and ordering.
- [ ] Reduce DuckDB-to-runtime marshalling overhead and Python object churn at scale.
- [ ] Accelerate fill/simulation core (especially synthetic execution model) with deterministic parity.
- [ ] Accelerate index build/normalization ETL workloads for large datasets.
- [ ] Implement mandatory parity gates comparing legacy and Rust paths on identical inputs (`markets_tested`, `final_value`, full `equity_curve`, full `trades`).
- [ ] Add explicit feature flags and rollback controls for every migrated slice.
- [ ] Preserve Python strategy authoring and avoid full-system rewrites.

### Out of Scope

- Rewriting the full backtest system in Rust — explicitly excluded to reduce regression risk and delivery time.
- Rust migration for PMXT sync HTTP/network path — low ROI because this path is network-bound.
- Rust migration for MCP/CLI orchestration glue — low ROI versus compute-path targets.
- Rewriting DuckDB SQL logic itself — low ROI because query execution is already native-fast.

## Context

- The current major bottleneck is Python CPU overhead in hot loops and data marshalling, not DuckDB query execution speed.
- Highest-ROI acceleration targets are currently ranked as:
  1. `_run_streaming` event executor hot loop (k-way merge, per-event updates, schedule checks, snapshot gating).
  2. DuckDB-to-runtime marshalling boundary (`PricePoint`/`Market` object churn and row-iteration overhead).
  3. Fill/simulation core (deterministic fill math and synthetic execution model logic).
  4. Index build/normalization ETL transforms.
- Existing architecture already centralizes critical flows in `agenttrader/core/backtest_engine.py`, `agenttrader/core/context.py`, and data adapters under `agenttrader/data/`.
- Technical concerns map highlights correctness and concurrency risk areas; performance changes must be isolated behind strict parity checks before rollout.

## Constraints

- **Scope**: No full rewrite — incremental adoption only.
- **Correctness**: Exact backtest correctness must be preserved versus legacy behavior.
- **API/UX**: Python strategy authoring must remain intact.
- **Rollout Safety**: Every migration slice must be feature-flagged with rollback path.
- **Measurement**: Each phase must define measurable performance impact and explicit success criteria.

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Prioritize Rust only for highest-ROI compute paths | Maximizes throughput gains while minimizing migration surface | — Pending |
| Gate rollout on strict parity validation against legacy path | Prevents silent behavioral drift in correctness-critical simulation | — Pending |
| Keep Python strategy interface stable | Avoids ecosystem disruption and keeps adoption incremental | — Pending |
| Exclude network-bound and orchestration layers from early Rust scope | Concentrates effort where CPU-bound wins are largest | — Pending |

---
*Last updated: 2026-03-04 after initialization*
