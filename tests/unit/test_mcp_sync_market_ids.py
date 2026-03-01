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
    client.get_candlesticks = lambda *_args, **_kwargs: [
        models.PricePoint(timestamp=1737392619, yes_price=0.99, no_price=0.01, volume=1500.0)
    ]
    client.get_orderbook_snapshots = lambda *_args, **_kwargs: []

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
