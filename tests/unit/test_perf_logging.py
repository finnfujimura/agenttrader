import importlib
import json
import time

import asyncio

import pytest

from agenttrader.cli.utils import json_errors
from agenttrader.perf_logging import log_performance_event

mcp_server = importlib.import_module("agenttrader.mcp.server")


def _read_lines(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_log_performance_event_writes_jsonl(tmp_path, monkeypatch):
    log_path = tmp_path / "performance.jsonl"
    monkeypatch.setenv("AGENTTRADER_PERF_LOG_PATH", str(log_path))

    started_at = time.time() - 1
    log_performance_event(
        source="cli",
        operation="agenttrader sync",
        started_at=started_at,
        duration_ms=250.0,
        status="ok",
        metadata={"example": True},
    )

    lines = _read_lines(log_path)
    assert len(lines) == 1
    assert lines[0]["source"] == "cli"
    assert lines[0]["operation"] == "agenttrader sync"
    assert lines[0]["status"] == "ok"
    assert lines[0]["duration_ms"] == 250.0
    assert lines[0]["metadata"]["example"] is True


def test_json_errors_wrapper_logs_cli_call(tmp_path, monkeypatch):
    log_path = tmp_path / "performance.jsonl"
    monkeypatch.setenv("AGENTTRADER_PERF_LOG_PATH", str(log_path))

    @json_errors
    def _sample_cmd(json_output=False):
        return {"ok": True, "json_output": json_output}

    _sample_cmd(json_output=True)
    lines = _read_lines(log_path)
    assert len(lines) == 1
    assert lines[0]["source"] == "cli"
    assert lines[0]["status"] == "ok"
    assert lines[0]["operation"] == "_sample_cmd"


def test_mcp_call_logs_tool_timing(tmp_path, monkeypatch):
    log_path = tmp_path / "performance.jsonl"
    monkeypatch.setenv("AGENTTRADER_PERF_LOG_PATH", str(log_path))
    monkeypatch.setattr(mcp_server, "is_initialized", lambda: False)

    asyncio.run(mcp_server.call_tool("get_markets", {}))
    lines = _read_lines(log_path)
    assert len(lines) >= 1
    last = lines[-1]
    assert last["source"] == "mcp"
    assert last["operation"] == "get_markets"
    assert last["status"] == "error"
    assert last["error"] == "NotInitialized"
