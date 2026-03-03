import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


mcp_server = importlib.import_module("agenttrader.mcp.server")
config_mod = importlib.import_module("agenttrader.config")


def _run(coro):
    return asyncio.run(coro)


class _FakeStdio:
    async def __aenter__(self):
        return ("read", "write")

    async def __aexit__(self, *_args):
        return False


@pytest.fixture(autouse=True)
def _stub_perf_logging(monkeypatch):
    monkeypatch.setattr(mcp_server, "log_performance_event", lambda **_kwargs: None)


def test_mcp_main_allows_startup_with_no_pmxt_sidecars(monkeypatch):
    called = {"run": False}

    async def fake_run(_read_stream, _write_stream, _options):
        called["run"] = True

    monkeypatch.setattr(config_mod, "DB_PATH", Path.cwd() / "__pmxt_guard_missing_db__.sqlite")
    monkeypatch.setattr(mcp_server, "stdio_server", lambda: _FakeStdio())
    monkeypatch.setattr(
        mcp_server,
        "server",
        SimpleNamespace(
            run=fake_run,
            create_initialization_options=lambda: {},
        ),
    )
    monkeypatch.setattr(mcp_server, "_list_process_command_lines", lambda: [])

    _run(mcp_server.main())

    assert called["run"] is True


def test_mcp_main_allows_startup_with_one_pmxt_sidecar(monkeypatch):
    called = {"run": False}

    async def fake_run(_read_stream, _write_stream, _options):
        called["run"] = True

    monkeypatch.setattr(config_mod, "DB_PATH", Path.cwd() / "__pmxt_guard_missing_db__.sqlite")
    monkeypatch.setattr(mcp_server, "stdio_server", lambda: _FakeStdio())
    monkeypatch.setattr(
        mcp_server,
        "server",
        SimpleNamespace(
            run=fake_run,
            create_initialization_options=lambda: {},
        ),
    )
    monkeypatch.setattr(
        mcp_server,
        "_list_process_command_lines",
        lambda: [
            {
                "pid": 1234,
                "command_line": (
                    r'node "C:\Users\finnf\repo-a\venv\Lib\site-packages\pmxt\_server\server\bundled.js" --port 4321'
                ),
            },
            {"pid": 9876, "command_line": "python agenttrader.py"},
        ],
    )

    _run(mcp_server.main())

    assert called["run"] is True


def test_mcp_main_fails_fast_with_duplicate_pmxt_sidecars(monkeypatch, capsys):
    monkeypatch.setattr(config_mod, "DB_PATH", Path.cwd() / "__pmxt_guard_missing_db__.sqlite")
    monkeypatch.setattr(mcp_server, "stdio_server", lambda: _FakeStdio())
    monkeypatch.setattr(
        mcp_server,
        "server",
        SimpleNamespace(
            run=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("server.run should not be reached")),
            create_initialization_options=lambda: {},
        ),
    )
    monkeypatch.setattr(
        mcp_server,
        "_list_process_command_lines",
        lambda: [
            {
                "pid": 1111,
                "command_line": (
                    r'node "C:\Users\finnf\repo-a\venv\Lib\site-packages\pmxt\_server\server\bundled.js" --port 4321'
                ),
            },
            {
                "pid": 2222,
                "command_line": (
                    '/usr/bin/node "/home/user/repo-b/.venv/lib/python3.12/site-packages/pmxt/_server/server/bundled.js"'
                ),
            },
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        _run(mcp_server.main())

    captured = capsys.readouterr()
    assert excinfo.value.code == 1
    assert "Multiple PMXT sidecar processes are running" in captured.err
    assert "wrong port/access-token pair" in captured.err
    assert "pid=1111" in captured.err
    assert "pid=2222" in captured.err


def test_pmxt_guard_blocks_lazy_pmxt_tool_when_duplicates_detected(monkeypatch):
    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: object())
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(
        mcp_server,
        "_list_process_command_lines",
        lambda: [
            {
                "pid": 1111,
                "command_line": (
                    r'node "C:\Users\finnf\repo-a\venv\Lib\site-packages\pmxt\_server\server\bundled.js" --port 4321'
                ),
            },
            {
                "pid": 2222,
                "command_line": (
                    r'node "C:\Users\finnf\repo-b\venv\Lib\site-packages\pmxt\_server\server\bundled.js" --port 5321'
                ),
            },
        ],
    )
    monkeypatch.setattr(
        mcp_server,
        "PmxtClient",
        lambda: (_ for _ in ()).throw(AssertionError("PmxtClient should not be created when the guard fails")),
    )

    result = _run(mcp_server.call_tool("match_markets", {}))
    payload = json.loads(result[0].text)

    assert payload["ok"] is False
    assert payload["error"] == "PmxtSidecarConflict"
    assert payload["sidecar_count"] == 2
    assert "port + access-token pairing" in payload["message"]
    assert payload["sidecars"][0]["pid"] == 1111
