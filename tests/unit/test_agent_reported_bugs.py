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


def test_index_provider_forwards_category_to_index():
    """IndexProvider.get_markets(category=X) should use the normalized index when available."""
    from agenttrader.data.index_provider import IndexProvider

    provider = IndexProvider.__new__(IndexProvider)
    provider._index = MagicMock()
    provider._index.is_available.return_value = True
    provider._index.get_markets.return_value = []
    provider._parquet = MagicMock()

    provider.get_markets(platform="polymarket", category="crypto", limit=50)

    provider._index.get_markets.assert_called_once_with(
        platform="polymarket", category="crypto", limit=50
    )
    provider._parquet.get_markets.assert_not_called()


def test_polymarket_category_inference_does_not_treat_generic_will_as_politics():
    """Generic 'Will ...' prompts should not be auto-classified as politics."""
    from agenttrader.data.parquet_adapter import ParquetDataAdapter

    inferred = ParquetDataAdapter._infer_polymarket_category(
        "will-sacramento-kings-win-the-2025-nba-finals",
        "Will the Sacramento Kings win the 2025 NBA Finals?",
    )

    assert inferred == "sports"


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
    monkeypatch.setattr(mcp_server, "get_all_sources", lambda: [(fake_source, "fake-source")])

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
    monkeypatch.setattr(mcp_server, "get_all_sources", lambda: [(fake_source, "fake-source")])

    result = _run(mcp_server.call_tool("research_markets", {
        "platform": "polymarket",
    }))
    payload = _payload(result)
    assert len(payload.get("markets", [])) == 1


# ---------------------------------------------------------------------------
# Bug 3: Parquet drops SQL LIMIT when category is active
# ---------------------------------------------------------------------------


def test_parquet_polymarket_category_adds_ilike_prefilter():
    """When category is provided, SQL should use ILIKE pre-filter and a bounded LIMIT."""
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
    # Category queries should stay bounded to avoid full scans
    assert "LIMIT" in sql
    # Params should contain the ILIKE patterns
    assert any("%bitcoin%" in str(p) for p in params), f"Expected bitcoin keyword in params, got {params}"
    assert params[-1] == 200


def test_parquet_polymarket_unknown_category_no_ilike():
    """Unknown polymarket categories should return quickly without querying."""
    from agenttrader.data.parquet_adapter import ParquetDataAdapter

    adapter = ParquetDataAdapter.__new__(ParquetDataAdapter)
    adapter._conn = MagicMock()
    adapter._poly_markets_view = "poly_markets"
    adapter._conn.execute.return_value.fetchall.return_value = []

    result = adapter._get_polymarket_markets(category="randomcategory", resolved_only=False, min_volume=None, limit=10)

    assert result == []
    adapter._conn.execute.assert_not_called()


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
         patch("agenttrader.core.paper_daemon.PmxtClient"), \
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
    monkeypatch.setattr(mcp_server, "get_all_sources", lambda: [])

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
    monkeypatch.setattr(mcp_server, "get_all_sources", lambda: [])

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


# ---------------------------------------------------------------------------
# Source fallback: get_all_sources
# ---------------------------------------------------------------------------


def test_get_all_sources_returns_sqlite_last(monkeypatch):
    """get_all_sources should return sqlite-cache as the last source."""
    from agenttrader.data.source_selector import get_all_sources

    class FakeIndex:
        def is_available(self):
            return True

    class FakeCache:
        pass

    monkeypatch.setattr("agenttrader.data.index_provider.IndexProvider", FakeIndex)
    monkeypatch.setattr("agenttrader.data.cache.DataCache", lambda _: FakeCache())
    monkeypatch.setattr("agenttrader.db.get_engine", lambda: None)

    sources = get_all_sources()
    names = [name for _, name in sources]
    assert names[-1] == "sqlite-cache"
    assert "normalized-index" in names


# ---------------------------------------------------------------------------
# Source fallback: get_price falls back to cache
# ---------------------------------------------------------------------------


def test_get_price_falls_back_to_cache(monkeypatch):
    """get_price should try cache when index returns None."""
    mcp_server = importlib.import_module("agenttrader.mcp.server")
    from agenttrader.data.models import PricePoint

    pp = PricePoint(timestamp=100, yes_price=0.55, no_price=0.45, volume=10)

    class FakeIndex:
        def get_latest_price(self, market_id, platform):
            return None  # No data in index

    class FakeCache:
        def get_latest_price(self, market_id):
            return pp

    monkeypatch.setattr(
        mcp_server, "get_all_sources",
        lambda: [(FakeIndex(), "normalized-index"), (FakeCache(), "sqlite-cache")],
    )

    result = _run(mcp_server.call_tool("get_price", {"market_id": "m1", "platform": "polymarket"}))
    payload = _payload(result)
    assert payload["ok"]
    assert payload["data_source"] == "sqlite-cache"
    assert payload["price"]["yes_price"] == 0.55


# ---------------------------------------------------------------------------
# Source fallback: get_history falls back to cache
# ---------------------------------------------------------------------------


def test_get_history_falls_back_to_cache(monkeypatch):
    """get_history should try cache when index returns empty list."""
    mcp_server = importlib.import_module("agenttrader.mcp.server")
    from agenttrader.data.models import PricePoint

    pp = PricePoint(timestamp=100, yes_price=0.60, no_price=0.40, volume=5)

    class FakeIndex:
        def get_price_history(self, market_id, platform, start, end):
            return []  # Empty in index

    class FakeCache:
        def get_market(self, market_id):
            return _make_market(market_id)

        def get_price_history(self, market_id, start, end):
            return [pp]

    monkeypatch.setattr(
        mcp_server, "get_all_sources",
        lambda: [(FakeIndex(), "normalized-index"), (FakeCache(), "sqlite-cache")],
    )

    result = _run(mcp_server.call_tool("get_history", {"market_id": "m1", "days": 7}))
    payload = _payload(result)
    assert payload["ok"]
    assert payload["data_source"] == "sqlite-cache"


def test_get_price_prefers_fresher_cache_over_stale_index(monkeypatch):
    """get_price should prefer the freshest timestamp, not the first non-empty source."""
    mcp_server = importlib.import_module("agenttrader.mcp.server")
    from agenttrader.data.models import PricePoint

    stale = PricePoint(timestamp=100, yes_price=0.25, no_price=0.75, volume=1)
    fresh = PricePoint(timestamp=200, yes_price=0.85, no_price=0.15, volume=2)

    class FakeIndex:
        def get_latest_price(self, market_id, platform):
            return stale

    class FakeCache:
        def get_latest_price(self, market_id):
            return fresh

    monkeypatch.setattr(
        mcp_server,
        "get_all_sources",
        lambda: [(FakeIndex(), "normalized-index"), (FakeCache(), "sqlite-cache")],
    )

    result = _run(mcp_server.call_tool("get_price", {"market_id": "m1", "platform": "polymarket"}))
    payload = _payload(result)
    assert payload["ok"]
    assert payload["data_source"] == "sqlite-cache"
    assert payload["price"]["yes_price"] == 0.85


def test_get_history_prefers_fresher_cache_over_stale_index(monkeypatch):
    """get_history should prefer the freshest non-empty source."""
    mcp_server = importlib.import_module("agenttrader.mcp.server")
    from agenttrader.data.models import PricePoint

    stale = PricePoint(timestamp=100, yes_price=0.25, no_price=0.75, volume=1)
    fresh = PricePoint(timestamp=200, yes_price=0.85, no_price=0.15, volume=2)

    class FakeIndex:
        def get_price_history(self, market_id, platform, start, end):
            return [stale]

    class FakeCache:
        def get_market(self, market_id):
            return _make_market(market_id)

        def get_price_history(self, market_id, start, end):
            return [fresh]

    monkeypatch.setattr(
        mcp_server,
        "get_all_sources",
        lambda: [(FakeIndex(), "normalized-index"), (FakeCache(), "sqlite-cache")],
    )

    result = _run(mcp_server.call_tool("get_history", {"market_id": "m1", "days": 7}))
    payload = _payload(result)
    assert payload["ok"]
    assert payload["data_source"] == "sqlite-cache"
    assert payload["analytics"]["current_price"] == 0.85


def test_research_markets_history_prefers_fresher_source(monkeypatch):
    """research_markets should use the freshest history data for analytics."""
    mcp_server = importlib.import_module("agenttrader.mcp.server")
    from agenttrader.data.models import PricePoint

    market = _make_market("m-active")
    stale = PricePoint(timestamp=100, yes_price=0.25, no_price=0.75, volume=1)
    fresh = PricePoint(timestamp=200, yes_price=0.85, no_price=0.15, volume=2)

    class FakeMarketSource:
        def get_markets(self, **kw):
            return [market]

    class FakeIndex:
        def get_price_history(self, market_id, platform, start, end):
            return [stale]

    class FakeCache:
        def get_market(self, market_id):
            return market

        def get_price_history(self, market_id, start, end):
            return [fresh]

    monkeypatch.setattr(mcp_server, "get_best_data_source", lambda: (FakeMarketSource(), "normalized-index"))
    monkeypatch.setattr(
        mcp_server,
        "get_all_sources",
        lambda: [(FakeIndex(), "normalized-index"), (FakeCache(), "sqlite-cache")],
    )

    result = _run(mcp_server.call_tool("research_markets", {"platform": "polymarket", "limit": 1}))
    payload = _payload(result)
    assert payload["ok"]
    assert payload["history"][0]["analytics"]["current_price"] == 0.85


def test_research_markets_flags_missing_lookback_history(monkeypatch):
    """research_markets should flag markets with no data in the requested lookback window."""
    mcp_server = importlib.import_module("agenttrader.mcp.server")

    market = _make_market("m-active")

    class FakeSource:
        def get_markets(self, **kw):
            return [market]

        def get_price_history(self, *a, **kw):
            return []

    monkeypatch.setattr(mcp_server, "get_best_data_source", lambda: (FakeSource(), "fake-source"))
    monkeypatch.setattr(mcp_server, "get_all_sources", lambda: [(FakeSource(), "fake-source")])

    result = _run(mcp_server.call_tool("research_markets", {"platform": "polymarket", "limit": 1}))
    payload = _payload(result)
    assert payload["ok"]
    assert payload["history"][0]["warning"] == "No price data found in the requested lookback window."
    assert payload["history"][0]["analytics"]["last_point_timestamp"] is None
    assert payload["history"][0]["analytics"]["has_24h_reference"] is False


# ---------------------------------------------------------------------------
# research_markets active_only filters resolved markets
# ---------------------------------------------------------------------------


def test_research_markets_active_only_filters_resolved(monkeypatch):
    """research_markets with active_only=true should exclude resolved markets."""
    mcp_server = importlib.import_module("agenttrader.mcp.server")

    active = _make_market("m-active")
    resolved = _make_market("m-resolved")
    resolved.resolved = True

    class FakeSource:
        def get_markets(self, **kw):
            return [active, resolved]

        def get_price_history(self, *a, **kw):
            return []

    monkeypatch.setattr(mcp_server, "get_best_data_source", lambda: (FakeSource(), "fake-source"))
    monkeypatch.setattr(mcp_server, "get_all_sources", lambda: [(FakeSource(), "fake-source")])

    result = _run(mcp_server.call_tool("research_markets", {
        "platform": "polymarket",
        "active_only": True,
    }))
    payload = _payload(result)
    market_ids = [m["id"] for m in payload.get("markets", [])]
    assert "m-active" in market_ids
    assert "m-resolved" not in market_ids


def test_research_markets_active_only_false_includes_resolved(monkeypatch):
    """research_markets with active_only=false should include resolved markets."""
    mcp_server = importlib.import_module("agenttrader.mcp.server")

    active = _make_market("m-active")
    resolved = _make_market("m-resolved")
    resolved.resolved = True

    class FakeSource:
        def get_markets(self, **kw):
            return [active, resolved]

        def get_price_history(self, *a, **kw):
            return []

    monkeypatch.setattr(mcp_server, "get_best_data_source", lambda: (FakeSource(), "fake-source"))
    monkeypatch.setattr(mcp_server, "get_all_sources", lambda: [(FakeSource(), "fake-source")])

    result = _run(mcp_server.call_tool("research_markets", {
        "platform": "polymarket",
        "active_only": False,
    }))
    payload = _payload(result)
    market_ids = [m["id"] for m in payload.get("markets", [])]
    assert "m-active" in market_ids
    assert "m-resolved" in market_ids


def test_research_markets_active_only_refetches_past_resolved_limit(monkeypatch):
    """research_markets should widen fetches when top-N markets are resolved."""
    mcp_server = importlib.import_module("agenttrader.mcp.server")

    resolved_1 = _make_market("m-resolved-1")
    resolved_1.resolved = True
    resolved_2 = _make_market("m-resolved-2")
    resolved_2.resolved = True
    active_1 = _make_market("m-active-1")
    active_2 = _make_market("m-active-2")
    ordered_markets = [resolved_1, resolved_2, active_1, active_2]
    requested_limits = []

    class FakeSource:
        def get_markets(self, **kw):
            requested_limits.append(int(kw["limit"]))
            return ordered_markets[: int(kw["limit"])]

        def get_price_history(self, *a, **kw):
            return []

    monkeypatch.setattr(mcp_server, "get_best_data_source", lambda: (FakeSource(), "fake-source"))
    monkeypatch.setattr(mcp_server, "get_all_sources", lambda: [(FakeSource(), "fake-source")])

    result = _run(mcp_server.call_tool("research_markets", {
        "platform": "polymarket",
        "limit": 2,
        "active_only": True,
    }))
    payload = _payload(result)

    assert payload["ok"] is True
    assert [m["id"] for m in payload.get("markets", [])] == ["m-active-1", "m-active-2"]
    assert requested_limits == [2, 10]


# ---------------------------------------------------------------------------
# sync_data zero with category filter includes warning
# ---------------------------------------------------------------------------


def test_sync_data_zero_with_category_includes_warning(monkeypatch):
    """sync_data with 0 results and category filter should include a warning."""
    mcp_server = importlib.import_module("agenttrader.mcp.server")

    class FakeClient:
        def get_markets(self, **kw):
            return []

    class FakeCache:
        pass

    monkeypatch.setattr(mcp_server, "PmxtClient", FakeClient)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _: FakeCache())
    monkeypatch.setattr(mcp_server, "get_all_sources", lambda: [])

    with patch.object(mcp_server, "get_engine"):
        result = _run(mcp_server.call_tool("sync_data", {
            "platform": "polymarket",
            "category": "nonexistent_category",
        }))

    payload = _payload(result)
    assert payload["ok"] is True
    assert "warning" in payload


def test_sync_data_processed_without_live_data_adds_warning(monkeypatch):
    """sync_data should warn when a market is processed but no live data is fetched."""
    mcp_server = importlib.import_module("agenttrader.mcp.server")

    class FakeClient:
        def get_markets(self, **kw):
            return [_make_market("m1")]

        def get_candlesticks(self, *a, **kw):
            return []

        def get_orderbook_snapshots(self, *a, **kw):
            return []

    class FakeCache:
        def upsert_market(self, _m):
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
        result = _run(mcp_server.call_tool("sync_data", {"platform": "polymarket", "limit": 1}))

    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["markets_synced"] == 1
    assert payload["markets_processed"] == 1
    assert payload["markets_with_live_data"] == 0
    assert "warning" in payload
    assert any(w["type"] == "NoLiveData" for w in payload["warnings"])
    assert payload["market_results"][0]["has_live_data"] is False
    assert payload["market_results"][0]["candles_status"] == "empty"
    assert payload["market_results"][0]["orderbook_status"] == "empty"


def test_sync_data_surfaces_pmxt_fetch_errors(monkeypatch):
    """sync_data should expose PMXT fetch errors in warnings instead of collapsing them silently."""
    mcp_server = importlib.import_module("agenttrader.mcp.server")

    class FakeClient:
        def get_markets(self, **kw):
            return [_make_market("m1")]

        def get_candlesticks_with_status(self, *a, **kw):
            return {"points": [], "status": "error", "error": "ohlcv boom"}

        def get_orderbook_snapshots_with_status(self, *a, **kw):
            return {"snapshots": [], "status": "error", "error": "book boom"}

    class FakeCache:
        def upsert_market(self, _m):
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
        result = _run(mcp_server.call_tool("sync_data", {"platform": "polymarket", "limit": 1}))

    payload = _payload(result)
    warning_types = {w["type"] for w in payload["warnings"]}
    assert payload["ok"] is True
    assert "CandlesFetchError" in warning_types
    assert "OrderbookFetchError" in warning_types
    assert "NoLiveData" in warning_types
    assert payload["market_results"][0]["candles_status"] == "error"
    assert payload["market_results"][0]["orderbook_status"] == "error"


# ---------------------------------------------------------------------------
# sync_data historical market_ids error mentions parquet
# ---------------------------------------------------------------------------


def test_sync_data_historical_market_ids_mentions_parquet(monkeypatch):
    """sync_data with unfound market_ids should mention parquet/get_history in the fix."""
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
            "market_ids": ["old-market-1"],
            "platform": "polymarket",
        }))

    payload = _payload(result)
    assert payload["ok"] is False
    assert "parquet" in payload["fix"].lower() or "get_history" in payload["fix"]
