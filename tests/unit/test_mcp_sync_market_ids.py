import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


mcp_server = importlib.import_module("agenttrader.mcp.server")
pmxt_client_mod = importlib.import_module("agenttrader.data.pmxt_client")
models = importlib.import_module("agenttrader.data.models")


def _run(coro):
    return asyncio.run(coro)


def test_sync_data_market_ids_uses_exact_lookup(monkeypatch):
    client = object.__new__(pmxt_client_mod.PmxtClient)
    client._poly = MagicMock()
    client._kalshi = MagicMock()
    client._kalshi.fetch_markets.return_value = [
        SimpleNamespace(
            market_id="internal-id",
            ticker="PRES-2024-DJT",
            title="Will Donald Trump or another Republican win the Presidency?",
            category="pres",
            tags=[],
            volume="2623342.07",
            resolution_date="2025-01-20T00:00:28Z",
            yes=SimpleNamespace(outcome_id="internal-id", price="1", label="yes"),
            no=SimpleNamespace(outcome_id="internal-id-no", price="0", label="no"),
            outcomes=[],
            active=False,
            closed=True,
        )
    ]
    client.get_candlesticks_with_status = lambda *_args, **_kwargs: {
        "points": [models.PricePoint(timestamp=1737392619, yes_price=0.99, no_price=0.01, volume=1500.0)],
        "status": "ok",
        "error": None,
    }
    client.get_orderbook_snapshots_with_status = lambda *_args, **_kwargs: {
        "snapshots": [],
        "status": "empty",
        "error": None,
    }

    class FakeCache:
        def __init__(self):
            self.markets = []
            self.points = []

        def upsert_market(self, market):
            self.markets.append(market)

        def upsert_price_points_batch(self, market_id, platform, batch, **_kwargs):
            self.points.append((market_id, platform, list(batch)))

        def mark_market_synced(self, _market_id, _timestamp):
            return None

    class FakeOrderBookStore:
        def write(self, *_args, **_kwargs):
            return 0

    shared_cache = FakeCache()
    monkeypatch.setenv("AGENTTRADER_PERF_LOG_PATH", str(Path(".pytest_perf_sync_market_ids.jsonl").resolve()))
    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: shared_cache)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_all_sources", lambda: [])
    monkeypatch.setattr(mcp_server, "PmxtClient", lambda: client)
    monkeypatch.setattr(mcp_server, "OrderBookStore", lambda: FakeOrderBookStore())

    result = _run(
        mcp_server.call_tool(
            "sync_data",
            {"platform": "kalshi", "market_ids": ["PRES-2024-DJT"], "days": 1, "limit": 1},
        )
    )
    payload = importlib.import_module("json").loads(result[0].text)

    client._kalshi.fetch_markets.assert_called_once_with(query="PRES-2024-DJT", status="all", limit=20)
    assert payload["ok"] is True
    assert payload["markets_synced"] == 1
    assert payload["price_points_fetched"] == 1
    assert payload["errors"] == []
    assert len(shared_cache.markets) == 1
    assert shared_cache.markets[0].platform == models.Platform.KALSHI


def test_sync_data_market_ids_prefers_local_metadata(monkeypatch):
    local_market = models.Market(
        id="poly-yes-token",
        condition_id="poly-condition-id",
        platform=models.Platform.POLYMARKET,
        title="Polymarket market",
        category="politics",
        tags=[],
        market_type=models.MarketType.BINARY,
        volume=123.0,
        close_time=9999999999,
        resolved=False,
        resolution=None,
        scalar_low=None,
        scalar_high=None,
    )

    class FakeSource:
        def get_markets_by_ids(self, market_ids, platform="all"):
            assert market_ids == ["poly-yes-token"]
            assert platform == "polymarket"
            return [local_market]

    class FakeClient:
        def __init__(self):
            self.candle_args = None
            self.orderbook_args = None

        def get_markets(self, **_kw):
            raise AssertionError("PmxtClient.get_markets should not run when local metadata resolves the market_id")

        def get_candlesticks(self, cond_id, platform, start, end, interval):
            self.candle_args = (cond_id, platform, start, end, interval)
            return [models.PricePoint(timestamp=1737392619, yes_price=0.55, no_price=0.45, volume=25.0)]

        def get_orderbook_snapshots(self, market_id, platform, start, end, limit):
            self.orderbook_args = (market_id, platform, start, end, limit)
            return []

    class FakeCache:
        def __init__(self):
            self.markets = []
            self.points = []
            self.synced = []

        def upsert_market(self, market):
            self.markets.append(market)

        def upsert_price_points_batch(self, market_id, platform, batch, **_kwargs):
            self.points.append((market_id, platform, list(batch)))

        def mark_market_synced(self, market_id, timestamp):
            self.synced.append((market_id, timestamp))

    class FakeOrderBookStore:
        def write(self, *_args, **_kwargs):
            return 0

    client = FakeClient()
    shared_cache = FakeCache()
    monkeypatch.setattr(mcp_server, "is_initialized", lambda: True)
    monkeypatch.setattr(mcp_server, "DataCache", lambda _engine: shared_cache)
    monkeypatch.setattr(mcp_server, "get_engine", lambda: object())
    monkeypatch.setattr(mcp_server, "get_all_sources", lambda: [(FakeSource(), "normalized-index")])
    monkeypatch.setattr(mcp_server, "PmxtClient", lambda: client)
    monkeypatch.setattr(mcp_server, "OrderBookStore", lambda: FakeOrderBookStore())

    result = _run(
        mcp_server.call_tool(
            "sync_data",
            {"platform": "polymarket", "market_ids": ["poly-yes-token"], "days": 1, "limit": 1},
        )
    )
    payload = importlib.import_module("json").loads(result[0].text)

    assert payload["ok"] is True
    assert payload["markets_synced"] == 1
    assert payload["price_points_fetched"] == 1
    assert client.candle_args is not None
    assert client.candle_args[0] == "poly-condition-id"
    assert client.orderbook_args is not None
    assert client.orderbook_args[0] == "poly-yes-token"
    assert shared_cache.points[0][0] == "poly-yes-token"
    assert shared_cache.points[0][1] == "polymarket"
