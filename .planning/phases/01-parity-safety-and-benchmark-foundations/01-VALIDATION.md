---
phase: 01
slug: parity-safety-and-benchmark-foundations
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-04
---

# Phase 01 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.x |
| **Config file** | `pyproject.toml` |
| **Quick run command** | `venv\\Scripts\\python.exe -m pytest --basetemp=codex_tmp_test tests\\unit\\test_backtest_*.py tests\\unit\\test_mcp_*.py` |
| **Full suite command** | `venv\\Scripts\\python.exe -m pytest --basetemp=codex_tmp_test tests\\unit tests\\integration` |
| **Estimated runtime** | ~120 seconds |

---

## Sampling Rate

- **After every task commit:** Run `venv\\Scripts\\python.exe -m pytest --basetemp=codex_tmp_test tests\\unit\\test_backtest_*.py tests\\unit\\test_mcp_*.py`
- **After every plan wave:** Run `venv\\Scripts\\python.exe -m pytest --basetemp=codex_tmp_test tests\\unit tests\\integration`
- **Before `$gsd-verify-work`:** Full suite must be green
- **Max feedback latency:** 180 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 01-01-01 | 01 | 1 | PARI-01, PARI-02 | unit | `venv\\Scripts\\python.exe -m pytest --basetemp=codex_tmp_test tests\\unit\\test_backtest_engine.py` | ✅ | ⬜ pending |
| 01-01-02 | 01 | 1 | PARI-03, PARI-04, PARI-05 | unit | `venv\\Scripts\\python.exe -m pytest --basetemp=codex_tmp_test tests\\unit\\test_backtest_engine.py tests\\unit\\test_backtest_artifacts.py` | ✅ | ⬜ pending |
| 01-02-01 | 02 | 1 | SAFE-01, SAFE-02, SAFE-04 | unit | `venv\\Scripts\\python.exe -m pytest --basetemp=codex_tmp_test tests\\unit\\test_cli_backtest.py tests\\unit\\test_mcp_server.py` | ✅ | ⬜ pending |
| 01-02-02 | 02 | 1 | SAFE-01, SAFE-02, SAFE-04 | integration | `venv\\Scripts\\python.exe -m pytest --basetemp=codex_tmp_test tests\\integration\\test_full_workflow.py` | ✅ | ⬜ pending |
| 01-03-01 | 03 | 2 | BENC-01, BENC-02 | integration | `venv\\Scripts\\python.exe -m pytest --basetemp=codex_tmp_test tests\\integration\\test_full_workflow.py` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/unit/test_phase1_parity_contract.py` — parity comparator and mismatch diagnostics coverage for PARI-01..05
- [ ] `tests/unit/test_phase1_rollout_flags.py` — feature-flag and fallback policy coverage for SAFE-01, SAFE-02, SAFE-04
- [ ] `tests/integration/test_phase1_benchmark_gates.py` — benchmark suite and threshold policy coverage for BENC-01, BENC-02
- [ ] `tests/fixtures/phase1_backtest_inputs/` — canonical parity benchmark datasets and expected hashes

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| CLI/MCP operator UX clarity for active path + guard state summaries | SAFE-04 | Messaging quality and operator clarity are difficult to assert fully with automation | Run one parity-pass and one parity-fail scenario from both CLI and MCP, confirm status/output fields are understandable and actionable |
| Benchmark reproducibility protocol sanity on target machine profile | BENC-01, BENC-02 | Environment variance and hardware pinning assumptions need human review | Execute benchmark workflow twice on designated baseline host and verify stability within configured variance bounds |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 180s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
