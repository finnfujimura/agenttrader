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

    result = _run(mcp_server.call_tool("get_price", {"market_id": "0xabc"}))
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["data_source"] == "normalized-index"
    assert payload["price"]["yes_price"] == 0.65


def test_unknown_tool_returns_fix(monkeypatch):
    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    result = _run(mcp_server.call_tool("unknown_tool", {}))
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"] == "UnknownTool"
    assert "list_tools" in payload["fix"]


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

    result = _run(mcp_server.call_tool("get_history", {"market_id": "0xabc", "days": 7}))
    payload = _payload(result)
    assert payload["ok"] is True
    assert "history" not in payload
    assert payload["analytics"]["current_price"] == 0.50
    assert payload["analytics"]["price_change_24h"] == pytest.approx(0.05)
    assert payload["analytics"]["trend_direction"] == "up"
    assert payload["analytics"]["points"] == 3


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

    result = _run(mcp_server.call_tool(
        "get_history",
        {"market_id": "0xdef", "days": 7, "include_raw": True},
    ))
    payload = _payload(result)
    assert payload["ok"] is True
    assert "history" in payload
    assert len(payload["history"]) == 1


def test_list_tools_includes_compound_tools():
    tools = _run(mcp_server.list_tools())
    names = {t.name for t in tools}
    assert "research_markets" in names
    assert "validate_and_backtest" in names


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

    result = _run(mcp_server.call_tool(
        "research_markets",
        {"platform": "polymarket", "days": 7, "limit": 5, "sync_first": True},
    ))
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["count"] == 1
    assert payload["sync"]["markets_synced"] == 1
    assert len(payload["history"]) == 1
