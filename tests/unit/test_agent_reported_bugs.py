"""Tests for agent-reported MCP bugs (agent_test_errors.md).

1. IndexProvider category forwarding
2. research_markets market_ids filtering
3. Parquet overfetch on category
4. Daemon strategy error caught (on_market_data doesn't crash)
5. Daemon crash marks portfolio "failed" in DB
6. list_paper_trades repairs stale "running" rows
7. sync_data zero markets returns ok=False
8. sync_data forwards category/resolved/granularity
9. CLI paper start uses proc.pid not Popen
"""

import asyncio
import importlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest

from agenttrader.data.models import Market, MarketType, Platform


def _run(coro):
    return asyncio.run(coro)


def _payload(result):
    return json.loads(result[0].text)


@pytest.fixture(autouse=True)
def _set_perf_log_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTTRADER_PERF_LOG_PATH", str(tmp_path / "performance.jsonl"))


def _make_market(mid="m1", platform=Platform.POLYMARKET, category="crypto"):
    return Market(
        id=mid,
        condition_id=mid,
        platform=platform,
        title="Test market",
        category=category,
        tags=[],
        market_type=MarketType.BINARY,
        volume=1000.0,
        close_time=0,
        resolved=False,
        resolution=None,
        scalar_low=None,
        scalar_high=None,
    )


# ---------------------------------------------------------------------------
# Bug 1: IndexProvider.get_markets() forwards category
# ---------------------------------------------------------------------------


def test_index_provider_forwards_category():
    """IndexProvider.get_markets(category=X) must forward category to parquet adapter."""
    from agenttrader.data.index_provider import IndexProvider

    provider = IndexProvider.__new__(IndexProvider)
    provider._parquet = MagicMock()
    provider._parquet.get_markets.return_value = []

    provider.get_markets(platform="polymarket", category="crypto", limit=50)

    provider._parquet.get_markets.assert_called_once_with(
        platform="polymarket", category="crypto", limit=50
    )


# ---------------------------------------------------------------------------
# Bug 2: research_markets post-filters by market_ids
# ---------------------------------------------------------------------------


def test_research_markets_filters_by_market_ids(monkeypatch):
    """research_markets should post-filter markets by market_ids when provided."""
    mcp_server = importlib.import_module("agenttrader.mcp.server")

    m1 = _make_market("m1")
    m2 = _make_market("m2")
    m3 = _make_market("m3")

    class FakeSource:
        def get_markets(self, **kw):
            return [m1, m2, m3]

        def get_price_history(self, *a, **kw):
            return []

    fake_source = FakeSource()
    monkeypatch.setattr(mcp_server, "get_best_data_source", lambda: (fake_source, "fake-source"))

    result = _run(mcp_server.call_tool("research_markets", {
        "platform": "polymarket",
        "market_ids": ["m1", "m3"],
    }))
    payload = _payload(result)
    market_ids = [m["id"] for m in payload.get("markets", [])]
    assert "m2" not in market_ids
    assert "m1" in market_ids
    assert "m3" in market_ids


# ---------------------------------------------------------------------------
# Bug 3: Parquet overfetch when category is set
# ---------------------------------------------------------------------------


def test_parquet_polymarket_overfetch_on_category():
    """When category is provided, SQL LIMIT should be 10x the requested limit."""
    from agenttrader.data.parquet_adapter import ParquetDataAdapter

    adapter = ParquetDataAdapter.__new__(ParquetDataAdapter)
    adapter._conn = MagicMock()
    adapter._poly_markets_view = "poly_markets"
    adapter._conn.execute.return_value.fetchall.return_value = []

    adapter._get_polymarket_markets(category="crypto", resolved_only=False, min_volume=None, limit=10)

    # The last param should be sql_limit = 10 * 10 = 100
    call_args = adapter._conn.execute.call_args
    params = call_args[0][1]
    assert params[-1] == 100, f"Expected SQL limit of 100 (10*10), got {params[-1]}"


def test_parquet_polymarket_no_overfetch_without_category():
    """Without category, SQL LIMIT should equal the requested limit."""
    from agenttrader.data.parquet_adapter import ParquetDataAdapter

    adapter = ParquetDataAdapter.__new__(ParquetDataAdapter)
    adapter._conn = MagicMock()
    adapter._poly_markets_view = "poly_markets"
    adapter._conn.execute.return_value.fetchall.return_value = []

    adapter._get_polymarket_markets(category=None, resolved_only=False, min_volume=None, limit=10)

    call_args = adapter._conn.execute.call_args
    params = call_args[0][1]
    assert params[-1] == 10, f"Expected SQL limit of 10, got {params[-1]}"


def test_parquet_kalshi_overfetch_on_category():
    """Kalshi should also overfetch when category is set."""
    from agenttrader.data.parquet_adapter import ParquetDataAdapter

    adapter = ParquetDataAdapter.__new__(ParquetDataAdapter)
    adapter._conn = MagicMock()
    adapter._kalshi_markets_view = "kalshi_markets"
    adapter._conn.execute.return_value.fetchall.return_value = []

    adapter._get_kalshi_markets(category="politics", resolved_only=False, min_volume=None, limit=5)

    call_args = adapter._conn.execute.call_args
    params = call_args[0][1]
    assert params[-1] == 50, f"Expected SQL limit of 50 (5*10), got {params[-1]}"


# ---------------------------------------------------------------------------
# Bug 4: Daemon strategy error caught
# ---------------------------------------------------------------------------


def test_daemon_on_market_data_error_does_not_crash():
    """Exception in on_market_data should be caught, not crash the daemon."""
    from agenttrader.core.paper_daemon import PaperDaemon, DaemonRuntime

    daemon = PaperDaemon.__new__(PaperDaemon)
    daemon.portfolio_id = "test-p"
    daemon._emit_stdout = False

    runtime = DaemonRuntime()
    strategy = MagicMock()
    strategy.on_market_data.side_effect = ValueError("strategy bug")
    strategy.on_schedule.return_value = None
    strategy.on_stop.return_value = None
    runtime.strategy = strategy
    runtime.shutdown = False

    context = MagicMock()
    context.subscriptions = {
        "m1": _make_market("m1"),
    }
    latest = SimpleNamespace(yes_price=0.5)
    context._cache.get_latest_price.return_value = latest
    context.get_orderbook.return_value = None
    runtime.context = context

    daemon._runtime = runtime

    # Run one iteration then shutdown
    call_count = [0]
    original_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        call_count[0] += 1
        if call_count[0] >= 1:
            runtime.shutdown = True

    with patch("asyncio.sleep", fake_sleep):
        # Should not raise despite strategy.on_market_data raising ValueError
        asyncio.run(daemon._main_loop())

    # Strategy was called (and errored), but loop continued
    strategy.on_market_data.assert_called()


# ---------------------------------------------------------------------------
# Bug 5: Daemon crash marks portfolio "failed"
# ---------------------------------------------------------------------------


def test_daemon_run_marks_portfolio_failed_on_crash():
    """If _main_loop crashes, _run() should mark portfolio as 'failed'."""
    from agenttrader.core.paper_daemon import PaperDaemon, DaemonRuntime

    daemon = PaperDaemon.__new__(PaperDaemon)
    daemon.portfolio_id = "test-crash"
    daemon.initial_cash = 1000.0
    daemon._emit_stdout = False
    daemon._runtime = DaemonRuntime()

    fake_row = MagicMock()
    fake_row.status = "running"
    fake_session = MagicMock()
    fake_session.get.return_value = fake_row
    fake_session.__enter__ = MagicMock(return_value=fake_session)
    fake_session.__exit__ = MagicMock(return_value=False)

    with patch("agenttrader.core.paper_daemon.get_engine"), \
         patch("agenttrader.core.paper_daemon.get_session", return_value=fake_session), \
         patch("agenttrader.core.paper_daemon.DataCache"), \
         patch("agenttrader.core.paper_daemon.OrderBookStore"), \
         patch("agenttrader.core.paper_daemon.LiveContext"), \
         patch.object(daemon, "_load_strategy"), \
         patch.object(daemon, "_setup_file_watcher"), \
         patch("asyncio.run", side_effect=RuntimeError("boom")), \
         patch("signal.signal"):
        with pytest.raises(RuntimeError, match="boom"):
            daemon._run()

    assert fake_row.status == "failed"


# ---------------------------------------------------------------------------
# Bug 6: list_paper_trades repairs stale "running" rows
# ---------------------------------------------------------------------------


def test_list_paper_trades_repairs_stale_running(monkeypatch):
    """list_paper_trades should auto-correct dead-PID 'running' portfolios to 'dead'."""
    mcp_server = importlib.import_module("agenttrader.mcp.server")

    class FakePortfolio:
        def __init__(self, pid, status):
            self.id = f"p-{pid}"
            self.pid = pid
            self.status = status

    stale = FakePortfolio(99999, "running")
    alive = FakePortfolio(12345, "running")

    class FakeCache:
        def list_paper_portfolios(self):
            return [stale, alive]

    class FakeRow:
        status = "running"

    fake_session = MagicMock()
    fake_row = FakeRow()
    fake_session.get.return_value = fake_row
    fake_session.__enter__ = MagicMock(return_value=fake_session)
    fake_session.__exit__ = MagicMock(return_value=False)

    monkeypatch.setattr(mcp_server, "_pid_alive", lambda pid: pid == 12345)
    monkeypatch.setattr(mcp_server, "get_session", lambda _: fake_session)

    cache = FakeCache()
    monkeypatch.setattr(mcp_server, "DataCache", lambda _: cache)

    with patch.object(mcp_server, "get_engine"):
        result = _run(mcp_server.call_tool("list_paper_trades", {}))

    payload = _payload(result)
    statuses = {p["id"]: p["status"] for p in payload["portfolios"]}
    assert statuses["p-99999"] == "dead"
    assert statuses["p-12345"] == "running"


# ---------------------------------------------------------------------------
# Bug 7: sync_data zero markets returns ok=False
# ---------------------------------------------------------------------------


def test_sync_data_zero_markets_returns_error(monkeypatch):
    """sync_data with market_ids but 0 synced should return ok=False."""
    mcp_server = importlib.import_module("agenttrader.mcp.server")

    class FakeClient:
        def get_markets(self, **kw):
            return []

    class FakeCache:
        pass

    monkeypatch.setattr(mcp_server, "PmxtClient", FakeClient)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _: FakeCache())

    with patch.object(mcp_server, "get_engine"):
        result = _run(mcp_server.call_tool("sync_data", {
            "market_ids": ["m1", "m2"],
            "platform": "polymarket",
        }))

    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "NoMarketsFound"


# ---------------------------------------------------------------------------
# Bug 8: sync_data forwards category/resolved/granularity
# ---------------------------------------------------------------------------


def test_sync_data_forwards_new_params(monkeypatch):
    """sync_data should forward category, resolved, granularity to client."""
    mcp_server = importlib.import_module("agenttrader.mcp.server")

    captured_mk = {}
    captured_interval = {}

    class FakeClient:
        def get_markets(self, **kw):
            captured_mk.update(kw)
            return [_make_market("m1")]

        def get_candlesticks(self, cond_id, platform, start, end, interval):
            captured_interval["interval"] = interval
            return []

        def get_orderbook_snapshots(self, *a, **kw):
            return []

    class FakeCache:
        def upsert_market(self, m):
            pass

        def upsert_price_points_batch(self, *a, **kw):
            pass

        def mark_market_synced(self, *a):
            pass

    class FakeObStore:
        def write(self, *a):
            return 0

    monkeypatch.setattr(mcp_server, "PmxtClient", FakeClient)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _: FakeCache())
    monkeypatch.setattr(mcp_server, "OrderBookStore", FakeObStore)

    with patch.object(mcp_server, "get_engine"):
        result = _run(mcp_server.call_tool("sync_data", {
            "platform": "polymarket",
            "category": "crypto",
            "resolved": True,
            "granularity": "daily",
        }))

    payload = _payload(result)
    assert payload["ok"]
    assert captured_mk.get("category") == "crypto"
    assert captured_mk.get("resolved") is True
    assert captured_interval.get("interval") == 1440


# ---------------------------------------------------------------------------
# Bug 9: CLI paper start uses proc.pid not Popen
# ---------------------------------------------------------------------------


def test_cli_paper_start_uses_pid_not_popen():
    """paper start should store proc.pid (int), not the Popen object."""
    from agenttrader.cli.paper import paper_group
    from click.testing import CliRunner
    import tempfile, os

    # We just verify the source code stores proc.pid, not daemon.start_as_daemon() directly
    import inspect
    from agenttrader.cli import paper as paper_mod

    source = inspect.getsource(paper_mod)
    # Should have "proc = daemon.start_as_daemon()" and "pid = proc.pid"
    assert "proc = daemon.start_as_daemon()" in source
    assert "pid = proc.pid" in source
    # Should NOT have "pid = daemon.start_as_daemon()" (old buggy form)
    assert "pid = daemon.start_as_daemon()" not in source
