# Codebase Concerns

## Scope
This document captures current technical risks and fragile areas observed in the repository, with concrete file evidence for follow-up planning.

## High-Priority Concerns

### 1) Data-source-dependent behavior can drift
- The runtime explicitly switches across three backends: normalized index -> raw parquet -> sqlite cache (`agenttrader/data/source_selector.py`).
- Backtest and research behavior may change based on what data source is available at runtime (`agenttrader/core/backtest_engine.py`, `agenttrader/mcp/server.py`).
- Risk: parity regressions appear only in specific environments (for example, local cache-only vs index-enabled).
- Mitigation: keep parity tests that force each source path and assert equivalent outcomes for representative strategies.

### 2) Strategy safety is validation-based, not sandbox-based
- Strategy files are validated with AST checks (`agenttrader/cli/validate.py`) and then imported/executed in daemon process (`agenttrader/core/paper_daemon.py`).
- AST guards reduce attack surface but do not provide full process isolation.
- Risk: untrusted strategy code can still cause resource exhaustion or unexpected runtime behavior.
- Mitigation: document trust assumptions clearly and consider process/resource hardening for broader multi-tenant use.

### 3) Long-lived daemon lifecycle complexity
- Detached subprocess + watchdog reload + runtime status files are coordinated in one path (`agenttrader/core/paper_daemon.py`).
- Platform-specific process flags and signal handling branches increase edge cases (Windows vs non-Windows).
- Risk: stale runtime status, orphaned process behavior, or partial reload failures under repeated edits/crashes.
- Mitigation: add stress tests around repeated reload/crash/restart cycles and strengthen runtime health assertions.

### 4) Migration-tree split can cause schema drift confusion
- A top-level Alembic tree exists (`alembic/versions/`) and package-local migrations also exist (`agenttrader/db/migrations/versions/`).
- Current init flow references package-local migration execution paths (`agenttrader/cli/config.py`).
- Risk: contributors apply or edit the wrong migration tree, producing divergent environments.
- Mitigation: standardize one canonical migration path and explicitly deprecate or gate the other.

## Medium-Priority Concerns

### 5) Large historical dataset and index build remain operationally heavy
- README documents a ~36GB dataset and warns index build is RAM-intensive with at least 8GB recommended (`README.md`, `agenttrader/cli/dataset.py`, `agenttrader/data/index_builder.py`).
- Risk: users hit failure modes in low-resource environments; support burden increases.
- Mitigation: improve preflight checks and actionable failure messaging before long-running index operations.

### 6) PMXT sidecar dependency remains a runtime fragility
- Live data and paper trading depend on PMXT/Node sidecar availability (`agenttrader/data/pmxt_client.py`, `agenttrader/mcp/server.py`).
- There are explicit guards for sidecar conflicts, indicating known operational sharp edges (`agenttrader/mcp/server.py`).
- Risk: intermittent live failures due to sidecar/process conflicts outside Python control.
- Mitigation: keep sidecar diagnostics prominent and add clearer recovery workflows in CLI/MCP errors.

### 7) Integration coverage is comparatively narrow
- Unit coverage is broad across many files in `tests/unit/`.
- Integration path appears centered on `tests/integration/test_full_workflow.py`.
- Risk: cross-surface regressions (CLI + MCP + daemon interactions) may escape when behavior changes span modules.
- Mitigation: add targeted integration scenarios for data-source fallback, daemon reload behavior, and MCP error contracts.

## Watchlist
- Performance and storage growth for artifact/orderbook files (`agenttrader/data/backtest_artifacts.py`, `agenttrader/data/orderbook_store.py`).
- Contract stability for JSON outputs used by agents (`agenttrader/cli/utils.py`, `agenttrader/mcp/server.py`).
- Temp-path monkeypatching in tests could mask real filesystem edge cases (`tests/conftest.py`).

## Recommended Next Checks
1. Add/expand parity tests that execute each data backend path for the same strategy and assert stable metrics.
2. Add daemon lifecycle integration tests for repeated reload + failure + recovery loops.
3. Clarify and enforce one migration source of truth in docs and init checks.
