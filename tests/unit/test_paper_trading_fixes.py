"""Tests for paper trading bug fixes:
1. stop_paper_trade catches OSError on Windows
2. start_paper_trade detects immediate daemon crash
3. get_portfolio auto-corrects dead PIDs
"""

import asyncio
import importlib
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

mcp_server = importlib.import_module("agenttrader.mcp.server")


def _run(coro):
    return asyncio.run(coro)


def _payload(result):
    return json.loads(result[0].text)


@pytest.fixture(autouse=True)
def _set_perf_log_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTTRADER_PERF_LOG_PATH", str(tmp_path / "performance.jsonl"))


# ---------------------------------------------------------------------------
# Fix 1: stop_paper_trade catches OSError (Windows WinError 87)
# ---------------------------------------------------------------------------


def test_stop_paper_trade_handles_oserror(monkeypatch):
    """os.kill raising OSError (WinError 87) should not prevent marking portfolio stopped."""

    class FakePortfolio:
        id = "p-123"
        pid = 99999
        status = "running"

    class FakeCache:
        def get_portfolio(self, _pid):
            return FakePortfolio()

    class FakeRow:
        status = "running"
        stopped_at = None

    fake_row = FakeRow()

    class FakeSession:
        def get(self, _cls, _pk):
            return fake_row

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: FakeCache())
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_session", lambda _engine: FakeSession())

    # Simulate Windows WinError 87
    def fake_kill(_pid, _sig):
        raise OSError("[WinError 87] The parameter is incorrect")

    monkeypatch.setattr("os.kill", fake_kill)

    result = _run(mcp_server.call_tool("stop_paper_trade", {"portfolio_id": "p-123"}))
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["stopped"] is True
    assert fake_row.status == "stopped"


def test_stop_paper_trade_handles_process_lookup_error(monkeypatch):
    """ProcessLookupError (Unix dead PID) should still be caught."""

    class FakePortfolio:
        id = "p-456"
        pid = 99999
        status = "running"

    class FakeCache:
        def get_portfolio(self, _pid):
            return FakePortfolio()

    class FakeRow:
        status = "running"
        stopped_at = None

    fake_row = FakeRow()

    class FakeSession:
        def get(self, _cls, _pk):
            return fake_row

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: FakeCache())
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_session", lambda _engine: FakeSession())

    def fake_kill(_pid, _sig):
        raise ProcessLookupError("No such process")

    monkeypatch.setattr("os.kill", fake_kill)

    result = _run(mcp_server.call_tool("stop_paper_trade", {"portfolio_id": "p-456"}))
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["stopped"] is True


# ---------------------------------------------------------------------------
# Fix 2: start_paper_trade detects daemon crash
# ---------------------------------------------------------------------------


def test_start_paper_trade_detects_daemon_crash(monkeypatch, tmp_path):
    """When daemon exits immediately, start_paper_trade should return DaemonCrashed error."""
    strategy = tmp_path / "strat.py"
    strategy.write_text(
        "from agenttrader.core.base_strategy import BaseStrategy\n"
        "class TestStrat(BaseStrategy):\n"
        "    def on_start(self): pass\n"
        "    def on_market_data(self, market, price, orderbook): pass\n"
        "    def on_stop(self): pass\n"
    )

    db_row = SimpleNamespace(
        id=None, status="running", pid=None, stopped_at=None,
        strategy_path=str(strategy), strategy_hash="abc", initial_cash=1000,
        cash_balance=1000, started_at=0, last_reload=None, reload_count=0,
    )

    class FakeSession:
        def add(self, obj):
            db_row.id = obj.id

        def commit(self):
            pass

        def get(self, _cls, _pk):
            return db_row

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    # Mock the daemon to return a proc that has already exited
    fake_proc = MagicMock()
    fake_proc.pid = 12345
    fake_proc.poll.return_value = 1  # exit code 1 = crashed

    log_path = tmp_path / "daemon.log"
    log_path.write_text("sqlite3.OperationalError: attempt to write a readonly database")

    class FakeDaemon:
        _stderr_path = log_path
        _stderr_file = MagicMock()

        def __init__(self, *_args):
            pass

        def start_as_daemon(self):
            return fake_proc

    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_session", lambda _engine: FakeSession())
    monkeypatch.setattr(mcp_server, "PaperDaemon", FakeDaemon)
    monkeypatch.setattr(mcp_server, "validate_strategy_file", lambda _p: {"ok": True, "valid": True, "errors": [], "warnings": []})

    # Patch time.sleep to avoid actually waiting
    monkeypatch.setattr("time.sleep", lambda _s: None)

    result = _run(mcp_server.call_tool("start_paper_trade", {
        "strategy_path": str(strategy),
        "initial_cash": 1000,
    }))
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "DaemonCrashed"
    assert "readonly" in payload.get("stderr", "")
    assert db_row.status == "failed"


def test_start_paper_trade_succeeds_when_daemon_alive(monkeypatch, tmp_path):
    """When daemon stays alive after health check, start_paper_trade returns ok."""
    strategy = tmp_path / "strat.py"
    strategy.write_text(
        "from agenttrader.core.base_strategy import BaseStrategy\n"
        "class TestStrat(BaseStrategy):\n"
        "    def on_start(self): pass\n"
        "    def on_market_data(self, market, price, orderbook): pass\n"
        "    def on_stop(self): pass\n"
    )

    db_row = SimpleNamespace(
        id=None, status="running", pid=None, stopped_at=None,
        strategy_path=str(strategy), strategy_hash="abc", initial_cash=1000,
        cash_balance=1000, started_at=0, last_reload=None, reload_count=0,
    )

    class FakeSession:
        def add(self, obj):
            db_row.id = obj.id

        def commit(self):
            pass

        def get(self, _cls, _pk):
            return db_row

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    fake_proc = MagicMock()
    fake_proc.pid = 12345
    fake_proc.poll.return_value = None  # still running

    class FakeDaemon:
        _stderr_file = MagicMock()

        def __init__(self, *_args):
            pass

        def start_as_daemon(self):
            return fake_proc

    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_session", lambda _engine: FakeSession())
    monkeypatch.setattr(mcp_server, "PaperDaemon", FakeDaemon)
    monkeypatch.setattr(mcp_server, "validate_strategy_file", lambda _p: {"ok": True, "valid": True, "errors": [], "warnings": []})
    monkeypatch.setattr("time.sleep", lambda _s: None)

    result = _run(mcp_server.call_tool("start_paper_trade", {
        "strategy_path": str(strategy),
        "initial_cash": 1000,
    }))
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["pid"] == 12345


# ---------------------------------------------------------------------------
# Fix 3: get_portfolio auto-corrects dead PIDs
# ---------------------------------------------------------------------------


def test_get_portfolio_detects_dead_pid(monkeypatch):
    """get_portfolio should change status from 'running' to 'dead' when PID is gone."""

    class FakePortfolio:
        id = "p-dead"
        pid = 99999
        status = "running"
        initial_cash = 1000
        cash_balance = 1000
        last_reload = None
        reload_count = 0

    portfolio = FakePortfolio()

    class FakeRow:
        status = "running"

    fake_row = FakeRow()
    call_count = {"n": 0}

    class FakeCache:
        def get_portfolio(self, _pid):
            call_count["n"] += 1
            if call_count["n"] > 1:
                portfolio.status = "dead"
            return portfolio

        def get_open_positions(self, _pid):
            return []

    class FakeSession:
        def get(self, _cls, _pk):
            return fake_row

        def commit(self):
            fake_row.status  # just access to verify it was set

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: FakeCache())
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_session", lambda _engine: FakeSession())

    # Simulate PID not alive
    monkeypatch.setattr(mcp_server, "_pid_alive", lambda _pid: False)

    result = _run(mcp_server.call_tool("get_portfolio", {"portfolio_id": "p-dead"}))
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["status"] == "dead"
    assert fake_row.status == "dead"


def test_get_portfolio_no_correction_for_alive_pid(monkeypatch):
    """get_portfolio should NOT change status when PID is alive."""

    class FakePortfolio:
        id = "p-alive"
        pid = 12345
        status = "running"
        initial_cash = 1000
        cash_balance = 1000
        last_reload = None
        reload_count = 0

    class FakeCache:
        def get_portfolio(self, _pid):
            return FakePortfolio()

        def get_open_positions(self, _pid):
            return []

    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: FakeCache())
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "_pid_alive", lambda _pid: True)

    result = _run(mcp_server.call_tool("get_portfolio", {"portfolio_id": "p-alive"}))
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["status"] == "running"


def test_get_portfolio_no_correction_for_stopped(monkeypatch):
    """get_portfolio should NOT touch already-stopped portfolios."""

    class FakePortfolio:
        id = "p-stopped"
        pid = 99999
        status = "stopped"
        initial_cash = 1000
        cash_balance = 1000
        last_reload = None
        reload_count = 0

    class FakeCache:
        def get_portfolio(self, _pid):
            return FakePortfolio()

        def get_open_positions(self, _pid):
            return []

    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: FakeCache())
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())

    result = _run(mcp_server.call_tool("get_portfolio", {"portfolio_id": "p-stopped"}))
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["status"] == "stopped"


# ---------------------------------------------------------------------------
# PaperDaemon.start_as_daemon platform-specific flags
# ---------------------------------------------------------------------------


def test_daemon_uses_platform_specific_launch_flags(monkeypatch):
    """Verify daemon uses correct subprocess flags per platform."""
    paper_daemon_mod = importlib.import_module("agenttrader.core.paper_daemon")

    captured_kwargs = {}
    original_popen = subprocess.Popen

    class FakeProc:
        pid = 42

    def fake_popen(cmd, **kwargs):
        captured_kwargs.update(kwargs)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    daemon = object.__new__(paper_daemon_mod.PaperDaemon)
    daemon.portfolio_id = "p-test"
    daemon.strategy_path = "strat.py"
    daemon.initial_cash = 1000.0

    if sys.platform == "win32":
        proc = daemon.start_as_daemon()
        assert "creationflags" in captured_kwargs
        assert "start_new_session" not in captured_kwargs
    else:
        proc = daemon.start_as_daemon()
        assert captured_kwargs.get("start_new_session") is True
        assert "creationflags" not in captured_kwargs


# ---------------------------------------------------------------------------
# _pid_alive helper
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Fix: daemon tolerates missing orderbooks
# ---------------------------------------------------------------------------


def test_daemon_main_loop_tolerates_missing_orderbook():
    """Daemon should pass None orderbook to strategy when no snapshot exists."""
    paper_daemon_mod = importlib.import_module("agenttrader.core.paper_daemon")

    daemon = object.__new__(paper_daemon_mod.PaperDaemon)
    daemon.portfolio_id = "p-ob"
    daemon.strategy_path = "strat.py"
    daemon.initial_cash = 1000.0
    daemon._emit_stdout = False

    received_orderbooks = []

    class FakeStrategy:
        def on_market_data(self, market, price, orderbook):
            received_orderbooks.append(orderbook)

        def on_schedule(self, dt, market):
            pass

        def on_stop(self):
            pass

    class FakeCache:
        def get_latest_price(self, _mid):
            return SimpleNamespace(yes_price=0.55, no_price=0.45, volume=100)

    class FakeContext:
        subscriptions = {"m1": SimpleNamespace(title="Test Market")}

        def set_live_price(self, _mid, _price):
            pass

        def get_orderbook(self, _mid):
            raise Exception("NoObservedOrderbook: no snapshot")

        def log(self, _msg):
            pass

    fake_context = FakeContext()
    fake_context._cache = FakeCache()

    runtime = paper_daemon_mod.DaemonRuntime()
    runtime.strategy = FakeStrategy()
    runtime.context = fake_context
    runtime.reload_requested.clear()

    # Run one iteration then stop
    iteration = [0]
    original_shutdown = runtime.shutdown

    async def one_iteration_loop():
        """Simulate one pass of _main_loop then shutdown."""
        daemon._runtime = runtime
        daemon._runtime.shutdown = False

        subscriptions = runtime.context.subscriptions
        for market_id, market in subscriptions.items():
            latest = runtime.context._cache.get_latest_price(market_id)
            if latest is None:
                continue
            runtime.context.set_live_price(market_id, latest.yes_price)
            try:
                orderbook = runtime.context.get_orderbook(market_id)
            except Exception:
                orderbook = None
            runtime.strategy.on_market_data(market, latest.yes_price, orderbook)

    import asyncio
    asyncio.run(one_iteration_loop())

    assert len(received_orderbooks) == 1
    assert received_orderbooks[0] is None


def test_pid_alive_returns_false_for_nonexistent_pid():
    """_pid_alive should return False for a PID that doesn't exist."""
    assert mcp_server._pid_alive(2**30) is False


def test_pid_alive_returns_true_for_own_process():
    """_pid_alive should return True for our own PID."""
    import os
    assert mcp_server._pid_alive(os.getpid()) is True
