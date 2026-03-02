import asyncio
import json
import importlib
import time
from types import SimpleNamespace

import pytest

mcp_server = importlib.import_module("agenttrader.mcp.server")
models = importlib.import_module("agenttrader.data.models")


def _run(coro):
    return asyncio.run(coro)


def _payload(result):
    return json.loads(result[0].text)


@pytest.fixture(autouse=True)
def _set_perf_log_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTTRADER_PERF_LOG_PATH", str(tmp_path / "performance.jsonl"))


def test_call_tool_not_initialized_includes_fix(monkeypatch):
    monkeypatch.setattr(mcp_server, "is_initialized", lambda: False)
    result = _run(mcp_server.call_tool("get_markets", {}))
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "NotInitialized"
    assert payload["fix"] == "Run: agenttrader init"


def test_call_tool_missing_argument_returns_fix(monkeypatch):
    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    result = _run(mcp_server.call_tool("get_price", {}))
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "BadRequest"
    assert "market_id" in payload["message"]
    assert "market_id" in payload["fix"]


def test_call_tool_market_not_found_returns_fix(monkeypatch):
    class FakeSource:
        def get_latest_price(self, _market_id, _platform):
            return None

    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: object())
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_best_data_source", lambda: (FakeSource(), "sqlite-cache"))
    monkeypatch.setattr(mcp_server, "get_all_sources", lambda: [(FakeSource(), "sqlite-cache")])

    result = _run(mcp_server.call_tool("get_price", {"market_id": "0xabc"}))
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "MarketNotFound"
    assert "sync_data" in payload["fix"]


def test_get_price_routes_through_source_selector(monkeypatch):
    price_point = SimpleNamespace(timestamp=1000, yes_price=0.65, no_price=0.35, volume=500)

    class FakeSource:
        def get_latest_price(self, _market_id, _platform):
            return price_point

    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: object())
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_best_data_source", lambda: (FakeSource(), "normalized-index"))
    monkeypatch.setattr(mcp_server, "get_all_sources", lambda: [(FakeSource(), "normalized-index")])

    result = _run(mcp_server.call_tool("get_price", {"market_id": "0xabc"}))
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["data_source"] == "normalized-index"
    assert payload["price"]["yes_price"] == 0.65


def test_get_history_returns_analytics_by_default(monkeypatch):
    now = int(time.time())
    history = [
        SimpleNamespace(timestamp=now - (2 * 86400), yes_price=0.40, no_price=0.60, volume=1000),
        SimpleNamespace(timestamp=now - 3600, yes_price=0.45, no_price=0.55, volume=1200),
        SimpleNamespace(timestamp=now, yes_price=0.50, no_price=0.50, volume=1500),
    ]

    class FakeCache:
        def get_market(self, _market_id):
            return SimpleNamespace(id="0xabc")

        def get_price_history(self, _market_id, _start_ts, _end_ts):
            return history

    fake_cache = FakeCache()
    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: fake_cache)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_best_data_source", lambda: (fake_cache, "sqlite-cache"))
    monkeypatch.setattr(mcp_server, "get_all_sources", lambda: [(fake_cache, "sqlite-cache")])

    result = _run(mcp_server.call_tool("get_history", {"market_id": "0xabc", "days": 7}))
    payload = _payload(result)
    assert payload["ok"] is True
    assert "history" not in payload
    assert payload["analytics"]["current_price"] == 0.50
    assert payload["analytics"]["price_change_24h"] == pytest.approx(0.05)
    assert payload["analytics"]["trend_direction"] == "up"
    assert payload["analytics"]["points"] == 3
    assert payload["analytics"]["last_point_timestamp"] == now
    assert payload["analytics"]["hours_since_last_point"] == pytest.approx(0.0, abs=0.01)
    assert payload["analytics"]["has_24h_reference"] is True


def test_get_history_include_raw_true_returns_history(monkeypatch):
    now = int(time.time())
    history = [SimpleNamespace(timestamp=now, yes_price=0.55, no_price=0.45, volume=2000)]

    class FakeCache:
        def get_market(self, _market_id):
            return SimpleNamespace(id="0xdef")

        def get_price_history(self, _market_id, _start_ts, _end_ts):
            return history

    fake_cache = FakeCache()
    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: fake_cache)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_best_data_source", lambda: (fake_cache, "sqlite-cache"))
    monkeypatch.setattr(mcp_server, "get_all_sources", lambda: [(fake_cache, "sqlite-cache")])

    result = _run(mcp_server.call_tool(
        "get_history",
        {"market_id": "0xdef", "days": 7, "include_raw": True},
    ))
    payload = _payload(result)
    assert payload["ok"] is True
    assert "history" in payload
    assert len(payload["history"]) == 1


def test_validate_and_backtest_returns_validation_error_with_fix(monkeypatch):
    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(
        mcp_server,
        "validate_strategy_file",
        lambda _path: {"ok": True, "valid": False, "errors": [{"line": 1, "message": "bad"}], "warnings": []},
    )

    result = _run(mcp_server.call_tool(
        "validate_and_backtest",
        {"strategy_path": "./bad.py", "start_date": "2024-01-01", "end_date": "2024-01-02"},
    ))
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "StrategyValidationError"
    assert "fix" in payload
    assert "validation" in payload


def test_research_markets_runs_compound_flow(monkeypatch):
    now = int(time.time())
    market = models.Market(
        id="0xabc",
        condition_id="cond-1",
        platform=models.Platform.POLYMARKET,
        title="Test Market",
        category="politics",
        tags=["test"],
        market_type=models.MarketType.BINARY,
        volume=1000.0,
        close_time=now + 86400,
        resolved=False,
        resolution=None,
        scalar_low=None,
        scalar_high=None,
    )
    points = [
        models.PricePoint(timestamp=now - 7200, yes_price=0.40, no_price=0.60, volume=100),
        models.PricePoint(timestamp=now, yes_price=0.45, no_price=0.55, volume=110),
    ]

    class FakeCache:
        def __init__(self):
            self.markets = {}
            self.history = {}

        def upsert_market(self, m):
            self.markets[m.id] = m

        def upsert_price_points_batch(self, market_id, _platform, batch, **_kwargs):
            self.history.setdefault(market_id, [])
            self.history[market_id].extend(batch)
            self.history[market_id].sort(key=lambda p: p.timestamp)

        def mark_market_synced(self, _market_id, _timestamp):
            return None

        def get_markets(self, platform="all", category=None, tags=None, limit=20, **_kwargs):
            rows = list(self.markets.values())
            if platform != "all":
                rows = [m for m in rows if m.platform.value == platform]
            if category:
                rows = [m for m in rows if m.category == category]
            if tags:
                tagset = set(tags)
                rows = [m for m in rows if tagset.issubset(set(m.tags))]
            return rows[:limit]

        def get_market(self, market_id):
            return self.markets.get(market_id)

        def get_price_history(self, market_id, start_ts, end_ts):
            return [
                p
                for p in self.history.get(market_id, [])
                if start_ts <= p.timestamp <= end_ts
            ]

    class FakeClient:
        def get_markets(self, **_kwargs):
            return [market]

        def get_candlesticks(self, *_args, **_kwargs):
            return points

        def get_orderbook_snapshots(self, *_args, **_kwargs):
            return []

    class FakeOrderBookStore:
        def write(self, *_args, **_kwargs):
            return 0

    shared_cache = FakeCache()
    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: shared_cache)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "PmxtClient", lambda: FakeClient())
    monkeypatch.setattr(mcp_server, "OrderBookStore", lambda: FakeOrderBookStore())
    monkeypatch.setattr(mcp_server, "get_best_data_source", lambda: (shared_cache, "sqlite-cache"))
    monkeypatch.setattr(mcp_server, "get_all_sources", lambda: [(shared_cache, "sqlite-cache")])

    result = _run(mcp_server.call_tool(
        "research_markets",
        {"platform": "polymarket", "days": 7, "limit": 5, "sync_first": True},
    ))
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["count"] == 1
    assert payload["sync"]["markets_synced"] == 1
    assert len(payload["history"]) == 1
    # Change 5: inline analytics on market objects
    assert "analytics" in payload["markets"][0]
    assert payload["markets"][0]["analytics"]["points"] == 2


def test_internal_error_not_classified_as_strategy_error(monkeypatch):
    """Change 2: Non-AgentTraderError/RuntimeError should be InternalError, not StrategyError."""
    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: object())
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_best_data_source", lambda: (object(), "sqlite-cache"))
    monkeypatch.setattr(mcp_server, "get_all_sources", lambda: [(object(), "sqlite-cache")])

    # Force a TypeError (internal bug, not strategy issue) inside run_backtest
    original_spec = importlib.util.spec_from_file_location

    def bad_spec(*_args, **_kwargs):
        raise TypeError("unexpected None")

    monkeypatch.setattr(importlib.util, "spec_from_file_location", bad_spec)
    # Patch validate_strategy_file + DB dependencies for validate_and_backtest path
    monkeypatch.setattr(
        mcp_server,
        "validate_strategy_file",
        lambda _path: {"ok": True, "valid": True, "errors": [], "warnings": []},
    )
    monkeypatch.setattr(mcp_server, "write_backtest_artifact", lambda *a, **kw: None)

    # Stub get_session to avoid real DB
    from contextlib import contextmanager

    @contextmanager
    def fake_session(_engine):
        class FakeSession:
            def add(self, _obj):
                pass

            def commit(self):
                pass

            def get(self, _cls, _id):
                return None

        yield FakeSession()

    monkeypatch.setattr(mcp_server, "get_session", fake_session)

    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write("# empty strategy\n")
        f.flush()
        strategy_path = f.name
    try:
        result = _run(mcp_server.call_tool(
            "run_backtest",
            {"strategy_path": strategy_path, "start_date": "2024-01-01", "end_date": "2024-01-02"},
        ))
        payload = _payload(result)
        assert payload["ok"] is False
        assert payload["error"] == "InternalError"
        assert "fix" in payload
    finally:
        os.unlink(strategy_path)
        monkeypatch.setattr(importlib.util, "spec_from_file_location", original_spec)


def test_research_markets_min_history_points_filters(monkeypatch):
    """Change 3: min_history_points should filter out markets with insufficient data."""
    now = int(time.time())
    market_good = models.Market(
        id="0xgood", condition_id="cond-1", platform=models.Platform.POLYMARKET,
        title="Good Market", category="politics", tags=["test"],
        market_type=models.MarketType.BINARY, volume=1000.0,
        close_time=now + 86400, resolved=False, resolution=None,
        scalar_low=None, scalar_high=None,
    )
    market_bad = models.Market(
        id="0xbad", condition_id="cond-2", platform=models.Platform.POLYMARKET,
        title="Bad Market", category="politics", tags=["test"],
        market_type=models.MarketType.BINARY, volume=500.0,
        close_time=now + 86400, resolved=False, resolution=None,
        scalar_low=None, scalar_high=None,
    )
    good_points = [
        models.PricePoint(timestamp=now - 7200, yes_price=0.40, no_price=0.60, volume=100),
        models.PricePoint(timestamp=now - 3600, yes_price=0.42, no_price=0.58, volume=110),
        models.PricePoint(timestamp=now, yes_price=0.45, no_price=0.55, volume=120),
    ]
    all_markets = {market_good.id: market_good, market_bad.id: market_bad}

    class FakeSource:
        def get_markets(self, **kwargs):
            return [market_good, market_bad]

        def get_market(self, market_id):
            return all_markets.get(market_id)

        def get_price_history(self, market_id, start_ts, end_ts):
            if market_id == "0xgood":
                return good_points
            return []  # bad market has no data

    fake_source = FakeSource()
    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: fake_source)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_best_data_source", lambda: (fake_source, "sqlite-cache"))
    monkeypatch.setattr(mcp_server, "get_all_sources", lambda: [(fake_source, "sqlite-cache")])
    monkeypatch.setattr(mcp_server, "_compute_capabilities", lambda markets, cache: {})

    result = _run(mcp_server.call_tool(
        "research_markets",
        {"platform": "polymarket", "days": 7, "limit": 10, "min_history_points": 2},
    ))
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["count"] == 1
    assert payload["markets"][0]["id"] == "0xgood"
    assert len(payload["history"]) == 1
    assert payload["history"][0]["market_id"] == "0xgood"


def test_research_markets_inline_analytics(monkeypatch):
    """Change 5: Each market object should have inline analytics."""
    now = int(time.time())
    market = models.Market(
        id="0xabc", condition_id="cond-1", platform=models.Platform.POLYMARKET,
        title="Test Market", category="politics", tags=["test"],
        market_type=models.MarketType.BINARY, volume=1000.0,
        close_time=now + 86400, resolved=False, resolution=None,
        scalar_low=None, scalar_high=None,
    )
    points = [
        models.PricePoint(timestamp=now - 3600, yes_price=0.40, no_price=0.60, volume=100),
        models.PricePoint(timestamp=now, yes_price=0.50, no_price=0.50, volume=200),
    ]

    class FakeSource:
        def get_markets(self, **kwargs):
            return [market]

        def get_market(self, market_id):
            return market if market_id == market.id else None

        def get_price_history(self, market_id, start_ts, end_ts):
            return points

    fake_source = FakeSource()
    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: fake_source)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_best_data_source", lambda: (fake_source, "sqlite-cache"))
    monkeypatch.setattr(mcp_server, "get_all_sources", lambda: [(fake_source, "sqlite-cache")])
    monkeypatch.setattr(mcp_server, "_compute_capabilities", lambda markets, cache: {})

    result = _run(mcp_server.call_tool(
        "research_markets",
        {"platform": "polymarket", "days": 7, "limit": 5},
    ))
    payload = _payload(result)
    assert payload["ok"] is True
    mkt = payload["markets"][0]
    assert "analytics" in mkt
    assert mkt["analytics"]["points"] == 2
    assert mkt["analytics"]["current_price"] == 0.50


def test_start_paper_trade_wait_for_ready(monkeypatch):
    """Change 4: wait_for_ready should poll and return positions when running."""
    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())

    from contextlib import contextmanager

    class FakeRow:
        def __init__(self):
            self.pid = None
            self.status = "running"
            self.cash_balance = 9500.0
            self.stopped_at = None

    fake_row = FakeRow()

    @contextmanager
    def fake_session(_engine):
        class FakeSession:
            def add(self, _obj):
                pass

            def commit(self):
                pass

            def get(self, _cls, _id):
                return fake_row

        yield FakeSession()

    monkeypatch.setattr(mcp_server, "get_session", fake_session)
    monkeypatch.setattr(
        mcp_server,
        "validate_strategy_file",
        lambda _path: {"ok": True, "valid": True, "errors": [], "warnings": []},
    )

    class FakeProc:
        def __init__(self):
            self.pid = 12345

        def poll(self):
            return None  # still alive

    call_count = [0]

    def fake_read_runtime_status(_portfolio_id):
        call_count[0] += 1
        if call_count[0] >= 2:
            return {"state": "running", "markets": []}
        return {"state": "starting"}

    monkeypatch.setattr(mcp_server, "read_runtime_status", fake_read_runtime_status)
    monkeypatch.setattr(mcp_server, "_pid_alive", lambda pid: True)

    class FakePosition:
        market_id = "0xabc"
        platform = "polymarket"
        side = "yes"
        contracts = 10.0
        avg_cost = 0.45

    class FakeCache:
        def get_open_positions(self, _portfolio_id):
            return [FakePosition()]

    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: FakeCache())

    class FakeDaemon:
        def __init__(self, *_a, **_kw):
            pass

        def start_as_daemon(self):
            return FakeProc()

    monkeypatch.setattr(mcp_server, "PaperDaemon", FakeDaemon)

    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write("# strategy\n")
        f.flush()
        strategy_path = f.name
    try:
        result = _run(mcp_server.call_tool(
            "start_paper_trade",
            {"strategy_path": strategy_path, "wait_for_ready": True},
        ))
        payload = _payload(result)
        assert payload["ok"] is True
        assert payload["cash_balance"] == 9500.0
        assert len(payload["positions"]) == 1
        assert payload["positions"][0]["market_id"] == "0xabc"
        assert payload["positions"][0]["contracts"] == 10.0
    finally:
        os.unlink(strategy_path)
