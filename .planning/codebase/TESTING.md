# Testing Patterns

## Test Stack and Entry Points
- Test framework is `pytest` (declared in `pyproject.toml` optional `dev` dependencies).
- Baseline command from `README.md`: `pytest -q`.
- Main suites:
  - `tests/unit/` (broad behavior/regression coverage)
  - `tests/integration/` (CLI workflow smoke path)
  - `tests/fixtures/` (sample strategy/market/orderbook fixtures)

## Current Suite Shape
- Unit tests are the dominant safety net (202 `test_` functions under `tests/unit/`).
- Integration currently centers on one end-to-end file: `tests/integration/test_full_workflow.py`.
- Unit files are mostly behavior- or bug-theme based, for example:
  - `tests/unit/test_agent_reported_bugs.py`
  - `tests/unit/test_audit_fixes.py`
  - `tests/unit/test_mcp_hardening.py`

## Global Test Fixtures and Isolation
- `tests/conftest.py` enforces deterministic temp behavior:
  - monkeypatches `tempfile.mkdtemp`, `tempfile.TemporaryDirectory`, and `tempfile.gettempdir`
  - provides a custom `tmp_path` fixture
  - routes temp files to `codex_tmp_test_runtime` under repo cwd
- `tests/conftest.py` also stubs PMXT sidecar process scans by default via `_list_process_command_lines`.

## Dominant Unit Testing Patterns
- Heavy use of `monkeypatch` for dependency seams and side-effect isolation (many files in `tests/unit/`).
- Frequent use of `unittest.mock` (`patch`, `MagicMock`) for targeted behavior checks (for example `tests/unit/test_mcp_source_routing.py`).
- Async MCP tool tests commonly use a local `_run(coro)` helper with `asyncio.run(...)`, then parse `result[0].text` as JSON.
- CLI tests use `click.testing.CliRunner` and assert both exit code and output channel behavior (see `tests/unit/test_backtest_progress.py`).
- Data adapter tests construct ephemeral DuckDB/SQLite state in `tmp_path` (see `tests/unit/test_parquet_adapter.py`, `tests/unit/test_schema_health.py`).

## Contract Assertion Style
- Assertions target explicit payload keys and semantics, not just truthiness:
  - `ok/error/message/fix`
  - domain fields like `data_source`, `execution_mode`, `progress_pct`
- Error-path tests prefer `pytest.raises(..., match=...)` with message checks.
- Regression tests often map to named issues and expected fixes (`tests/unit/test_audit_fixes.py`).

## Env and Path Overrides in Tests
- Tests commonly override runtime paths/env via `monkeypatch.setenv`:
  - `AGENTTRADER_PERF_LOG_PATH`
  - `AGENTTRADER_STATE_DIR`
  - `AGENTTRADER_DATA_ROOT`
- When adding tests that log or write runtime artifacts, prefer per-test temp paths over shared defaults.

## Integration Test Characteristics
- `tests/integration/test_full_workflow.py` runs the CLI through `subprocess.run` and `--json`.
- It performs real workflow steps (`init`, `sync`, `markets`, `validate`, `backtest`, `paper`) and may `pytest.skip(...)` when live market history is unavailable.
- Treat integration as environment-dependent smoke coverage; keep deterministic logic in unit tests.

## Adding New Tests Safely
- Start with unit tests in a nearby thematic file before extending integration coverage.
- Patch network/sidecar/process boundaries instead of calling live services in unit tests.
- Reuse existing helpers/patterns (`_run`, fake provider classes, `CliRunner`) to match suite style.
- Assert complete response contracts for MCP/CLI changes, not just a single field.
