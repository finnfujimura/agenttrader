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


def test_research_markets_direct_id_lookup(monkeypatch):
    """research_markets with market_ids should use get_markets_by_ids, not get_markets."""
    mcp_server = importlib.import_module("agenttrader.mcp.server")

    m1 = _make_market("m1")
    m3 = _make_market("m3")

    class FakeSource:
        def get_markets(self, **kw):
            # Should NOT be called when market_ids is provided
            raise AssertionError("get_markets should not be called when market_ids is set")

        def get_markets_by_ids(self, market_ids, platform="all"):
            return [m for m in [m1, m3] if m.id in market_ids]

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
    assert "m1" in market_ids
    assert "m3" in market_ids


def test_research_markets_without_ids_uses_get_markets(monkeypatch):
    """research_markets without market_ids should still use normal get_markets path."""
    mcp_server = importlib.import_module("agenttrader.mcp.server")

    m1 = _make_market("m1")

    class FakeSource:
        def get_markets(self, **kw):
            return [m1]

        def get_markets_by_ids(self, market_ids, platform="all"):
            raise AssertionError("get_markets_by_ids should not be called without market_ids")

        def get_price_history(self, *a, **kw):
            return []

    fake_source = FakeSource()
    monkeypatch.setattr(mcp_server, "get_best_data_source", lambda: (fake_source, "fake-source"))

    result = _run(mcp_server.call_tool("research_markets", {
        "platform": "polymarket",
    }))
    payload = _payload(result)
    assert len(payload.get("markets", [])) == 1


# ---------------------------------------------------------------------------
# Bug 3: Parquet drops SQL LIMIT when category is active
# ---------------------------------------------------------------------------


def test_parquet_polymarket_category_adds_ilike_prefilter():
    """When category is provided, SQL should have ILIKE pre-filter and no LIMIT."""
    from agenttrader.data.parquet_adapter import ParquetDataAdapter

    adapter = ParquetDataAdapter.__new__(ParquetDataAdapter)
    adapter._conn = MagicMock()
    adapter._poly_markets_view = "poly_markets"
    adapter._conn.execute.return_value.fetchall.return_value = []

    adapter._get_polymarket_markets(category="crypto", resolved_only=False, min_volume=None, limit=10)

    call_args = adapter._conn.execute.call_args
    sql = call_args[0][0]
    params = call_args[0][1]
    # Should have ILIKE pre-filter for crypto keywords (bitcoin, btc, ethereum, etc.)
    assert "ILIKE" in sql, f"SQL should have ILIKE pre-filter, got: {sql}"
    # Should NOT have LIMIT (Python trims after exact category check)
    assert "LIMIT" not in sql, f"SQL should not have LIMIT when polymarket category is set"
    # Params should contain the ILIKE patterns
    assert any("%bitcoin%" in str(p) for p in params), f"Expected bitcoin keyword in params, got {params}"


def test_parquet_polymarket_unknown_category_no_ilike():
    """Unknown polymarket categories (e.g. 'kxfeddecision') get no ILIKE pre-filter but still no LIMIT."""
    from agenttrader.data.parquet_adapter import ParquetDataAdapter

    adapter = ParquetDataAdapter.__new__(ParquetDataAdapter)
    adapter._conn = MagicMock()
    adapter._poly_markets_view = "poly_markets"
    adapter._conn.execute.return_value.fetchall.return_value = []

    adapter._get_polymarket_markets(category="randomcategory", resolved_only=False, min_volume=None, limit=10)

    call_args = adapter._conn.execute.call_args
    sql = call_args[0][0]
    # No known keywords -> no ILIKE, but still no LIMIT
    assert "ILIKE" not in sql
    assert "LIMIT" not in sql


def test_parquet_polymarket_has_sql_limit_without_category():
    """Without category, SQL LIMIT should equal the requested limit."""
    from agenttrader.data.parquet_adapter import ParquetDataAdapter

    adapter = ParquetDataAdapter.__new__(ParquetDataAdapter)
    adapter._conn = MagicMock()
    adapter._poly_markets_view = "poly_markets"
    adapter._conn.execute.return_value.fetchall.return_value = []

    adapter._get_polymarket_markets(category=None, resolved_only=False, min_volume=None, limit=10)

    call_args = adapter._conn.execute.call_args
    sql = call_args[0][0]
    params = call_args[0][1]
    assert "LIMIT" in sql
    assert params[-1] == 10, f"Expected SQL limit of 10, got {params[-1]}"


def test_parquet_kalshi_category_uses_sql_filter():
    """Kalshi category should push exact REGEXP filter into SQL and keep LIMIT."""
    from agenttrader.data.parquet_adapter import ParquetDataAdapter

    adapter = ParquetDataAdapter.__new__(ParquetDataAdapter)
    adapter._conn = MagicMock()
    adapter._kalshi_markets_view = "kalshi_markets"
    adapter._conn.execute.return_value.fetchall.return_value = []

    adapter._get_kalshi_markets(category="kxfeddecision", resolved_only=False, min_volume=None, limit=5)

    call_args = adapter._conn.execute.call_args
    sql = call_args[0][0]
    params = call_args[0][1]
    # Should have REGEXP_EXTRACT filter in SQL
    assert "REGEXP_EXTRACT" in sql, f"SQL should have REGEXP_EXTRACT filter, got: {sql}"
    # Should still have LIMIT (Kalshi filter is exact)
    assert "LIMIT" in sql
    # Category param should be in params
    assert "kxfeddecision" in params
    # LIMIT should be 5
    assert params[-1] == 5


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


# ---------------------------------------------------------------------------
# Parquet get_markets_by_ids
# ---------------------------------------------------------------------------


def test_parquet_get_markets_by_ids_polymarket():
    """get_markets_by_ids queries polymarket by token ID or condition_id."""
    from agenttrader.data.parquet_adapter import ParquetDataAdapter

    adapter = ParquetDataAdapter.__new__(ParquetDataAdapter)
    adapter._conn = MagicMock()
    adapter._poly_markets_view = "poly_markets"
    adapter._kalshi_markets_view = None
    # Return one row matching
    adapter._conn.execute.return_value.fetchall.return_value = [
        ("tok-1", "cond-1", "Test Q", "test-slug", 100.0, False, None, "0.50"),
    ]

    results = adapter.get_markets_by_ids(["tok-1"], platform="polymarket")
    assert len(results) == 1
    assert results[0].id == "tok-1"

    # Verify SQL uses IN clause, not LIMIT
    sql = adapter._conn.execute.call_args[0][0]
    assert "IN" in sql
    assert "LIMIT" not in sql


def test_parquet_get_markets_by_ids_kalshi():
    """get_markets_by_ids queries kalshi by ticker."""
    from agenttrader.data.parquet_adapter import ParquetDataAdapter

    adapter = ParquetDataAdapter.__new__(ParquetDataAdapter)
    adapter._conn = MagicMock()
    adapter._poly_markets_view = None
    adapter._kalshi_markets_view = "kalshi_markets"
    adapter._conn.execute.return_value.fetchall.return_value = [
        ("KXFED-DEC-T475", "KXFED", "Fed rate", "binary", "finalized", 500.0, None, "yes"),
    ]

    results = adapter.get_markets_by_ids(["KXFED-DEC-T475"], platform="kalshi")
    assert len(results) == 1
    assert results[0].id == "KXFED-DEC-T475"


def test_parquet_get_markets_by_ids_empty():
    """get_markets_by_ids with empty list returns empty."""
    from agenttrader.data.parquet_adapter import ParquetDataAdapter

    adapter = ParquetDataAdapter.__new__(ParquetDataAdapter)
    adapter._conn = MagicMock()
    results = adapter.get_markets_by_ids([], platform="all")
    assert results == []
    adapter._conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# IndexProvider get_markets_by_ids delegation
# ---------------------------------------------------------------------------


def test_index_provider_forwards_get_markets_by_ids():
    """IndexProvider.get_markets_by_ids delegates to parquet adapter."""
    from agenttrader.data.index_provider import IndexProvider

    provider = IndexProvider.__new__(IndexProvider)
    provider._parquet = MagicMock()
    provider._parquet.get_markets_by_ids.return_value = [_make_market("m1")]

    result = provider.get_markets_by_ids(["m1"], platform="kalshi")
    assert len(result) == 1
    provider._parquet.get_markets_by_ids.assert_called_once_with(
        market_ids=["m1"], platform="kalshi"
    )


# ---------------------------------------------------------------------------
# CacheProvider get_markets_by_ids
# ---------------------------------------------------------------------------


def test_cache_provider_get_markets_by_ids():
    """CacheProvider.get_markets_by_ids looks up each ID via cache.get_market."""
    from agenttrader.data.cache_provider import CacheProvider

    m1 = _make_market("m1", platform=Platform.KALSHI)
    fake_cache = MagicMock()
    fake_cache.get_market.side_effect = lambda mid: m1 if mid == "m1" else None

    provider = CacheProvider(fake_cache, MagicMock())
    result = provider.get_markets_by_ids(["m1", "m-missing"], platform="all")
    assert len(result) == 1
    assert result[0].id == "m1"


# ---------------------------------------------------------------------------
# get_markets MCP tool with market_ids
# ---------------------------------------------------------------------------


def test_get_markets_with_market_ids(monkeypatch):
    """get_markets tool should use direct ID lookup when market_ids is provided."""
    mcp_server = importlib.import_module("agenttrader.mcp.server")

    m1 = _make_market("m1")

    class FakeSource:
        def get_markets(self, **kw):
            raise AssertionError("get_markets should not be called")

        def get_markets_by_ids(self, market_ids, platform="all"):
            return [m1] if "m1" in market_ids else []

        def get_price_history(self, *a, **kw):
            return []

    monkeypatch.setattr(mcp_server, "get_best_data_source", lambda: (FakeSource(), "fake-source"))

    result = _run(mcp_server.call_tool("get_markets", {
        "market_ids": ["m1"],
    }))
    payload = _payload(result)
    assert payload["ok"]
    assert len(payload["markets"]) == 1
    assert payload["markets"][0]["id"] == "m1"
