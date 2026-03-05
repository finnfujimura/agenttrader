# Testing Guide

## Framework and current setup
- Test framework is `pytest` (dev dependency in `pyproject.toml`).
- Async/plugin support is present (`pytest-asyncio` in `pyproject.toml`; active plugin output appears in `test_results.txt`).
- Coverage tooling is available (`pytest-cov` in `pyproject.toml`) but no coverage threshold or report config is defined in `pyproject.toml`.
- There is no dedicated CI test workflow right now; `.github/workflows/publish.yml` only builds and publishes distributions.

## Test suite structure
- Shared fixtures and environment hardening live in `tests/conftest.py`.
- Unit tests are concentrated in `tests/unit/` and target adapters, core engine, MCP routing, validation, and regression fixes.
- End-to-end CLI workflow coverage lives in `tests/integration/test_full_workflow.py`.
- Reusable static fixtures are stored in `tests/fixtures/sample_markets.json`, `tests/fixtures/sample_orderbooks.json`, and `tests/fixtures/sample_strategy.py`.

## Fixture and isolation conventions
- `tests/conftest.py` overrides tempfile behavior with deterministic directories under `codex_tmp_test_runtime` for reproducible paths and cleanup.
- `tests/conftest.py` stubs PMXT sidecar process scanning globally via `_stub_pmxt_sidecar_scan` to avoid host-process nondeterminism.
- Many tests isolate filesystem/database state with per-test `tmp_path` and SQLite files (for example `tests/unit/test_mcp_hardening.py` and `tests/unit/test_schema_health.py`).
- Tests that could write logs set `AGENTTRADER_PERF_LOG_PATH` with `monkeypatch.setenv` (for example `tests/unit/test_perf_logging.py` and `tests/unit/test_paper_trading_fixes.py`).

## Mocking patterns used in this repo
- Preferred patching style is `pytest` `monkeypatch` for module attributes and environment variables (`tests/unit/test_mcp_error_fix.py`, `tests/unit/test_backtest_progress.py`).
- `unittest.mock.patch` and `MagicMock` are used when patching context managers or dynamic imports (`tests/unit/test_source_selector.py`, `tests/unit/test_mcp_hardening.py`).
- Source-selector tests inject fake modules with `patch.dict(sys.modules, ...)` in `tests/unit/test_source_selector.py`.
- Adapter/engine tests frequently use small fake classes instead of deep mocks to preserve behavior semantics (`tests/unit/test_backtest_streaming.py`, `tests/unit/test_agent_reported_bugs.py`).

## Integration testing behavior
- `tests/integration/test_full_workflow.py` executes the CLI with `subprocess.run` and creates a temporary strategy file, then exercises init/sync/markets/validate/backtest/paper.
- The integration path depends on live data availability and can skip when no market history exists (`pytest.skip` in `tests/integration/test_full_workflow.py`).
- Because integration tests are network/environment sensitive, unit tests are the primary deterministic gate for regressions.

## What is covered well
- Data-source routing, fallback, and provenance behavior (`tests/unit/test_source_selector.py`, `tests/unit/test_mcp_source_routing.py`, `tests/unit/test_mcp_research_no_sync.py`).
- Backtest behavior including strict/synthetic modes, streaming progress, and artifact handling (`tests/unit/test_strict_backtest.py`, `tests/unit/test_no_silent_synthetic.py`, `tests/unit/test_backtest_progress.py`, `tests/unit/test_backtest_streaming.py`).
- MCP hardening and error payload behavior (`tests/unit/test_mcp_hardening.py`, `tests/unit/test_mcp_error_fix.py`, `tests/unit/test_pmxt_sidecar_guard.py`).
- Strategy validation rules and forbidden imports (`tests/unit/test_strategy_validator.py`, `tests/unit/test_audit_fixes.py`).

## Coverage priorities to maintain
- Keep high-confidence tests on command/API boundary contracts in `agenttrader/cli/*.py` and `agenttrader/mcp/server.py` (payload shape, `ok/error/fix` semantics).
- Preserve regression-focused tests for execution correctness in `agenttrader/core/context.py` and `agenttrader/core/backtest_engine.py`.
- Continue adapter-level coverage for index/parquet/cache behavior in `agenttrader/data/index_adapter.py`, `agenttrader/data/parquet_adapter.py`, and `agenttrader/data/cache.py`.
- Maintain schema and path handling checks tied to `agenttrader/db/health.py` and `agenttrader/config.py`.

## Practical test-writing conventions
- Name tests by user-visible behavior or regression intent, matching existing style like `test_sync_data_processed_without_live_data_adds_warning` in `tests/unit/test_agent_reported_bugs.py`.
- Validate external behavior (returned payloads, persisted fields, warnings) before internal implementation details.
- Prefer deterministic timestamps, explicit fake responses, and local sqlite files for reproducibility (`tests/unit/test_mcp_hardening.py`).
- For async MCP entrypoints, wrap with local helper (`asyncio.run`) as used in `tests/unit/test_mcp_hardening.py` and `tests/unit/test_pmxt_sidecar_guard.py`.
- Keep side effects controlled through monkeypatching and temp directories rather than mutating global user state.

## Suggested execution commands
- Run core tests: `pytest -q`.
- Run only unit tests: `pytest -q tests/unit`.
- Run integration test explicitly when environment is ready: `pytest -q tests/integration/test_full_workflow.py`.
- Add coverage report when needed: `pytest --cov=agenttrader --cov-report=term-missing`.
