from agenttrader.data.cache_provider import CacheProvider
from agenttrader.data.models import (
    DataProvenance, Market, MarketType, OrderBook, OrderLevel,
    Platform, PricePoint,
)


def test_get_orderbook_returns_stored_snapshot():
    """CacheProvider returns real stored OB, never synthetic."""
    ob = OrderBook(
        market_id="m1",
        timestamp=100,
        bids=[OrderLevel(price=0.45, size=500)],
        asks=[OrderLevel(price=0.55, size=500)],
    )

    class FakeCache:
        def get_markets(self, **kwargs):
            return []
        def get_price_history(self, market_id, start_ts, end_ts, platform=None):
            return []
        def get_latest_price(self, market_id, platform=None):
            return PricePoint(timestamp=100, yes_price=0.50, no_price=0.50, volume=10)

    class FakeObStore:
        def get_nearest(self, platform, market_id, ts):
            return ob

    provider = CacheProvider.__new__(CacheProvider)
    provider._cache = FakeCache()
    provider._ob_store = FakeObStore()
    result = provider.get_orderbook("m1", "polymarket", 100)
    assert result is not None
    assert result.bids[0].size == 500


def test_get_orderbook_returns_none_when_no_stored():
    """CacheProvider returns None when no stored OB — never synthesizes."""
    class FakeCache:
        def get_markets(self, **kwargs):
            return []
        def get_latest_price(self, market_id, platform=None):
            return PricePoint(timestamp=100, yes_price=0.50, no_price=0.50, volume=10)

    class FakeObStore:
        def get_nearest(self, platform, market_id, ts):
            return None

    provider = CacheProvider.__new__(CacheProvider)
    provider._cache = FakeCache()
    provider._ob_store = FakeObStore()
    result = provider.get_orderbook("m1", "polymarket", 100)
    assert result is None


def test_provenance_reports_pmxt_observed():
    class FakeCache:
        def get_provenance(self, market_id, platform):
            _ = (market_id, platform)
            return DataProvenance(source="pmxt", observed=True, granularity="1h")

    provider = CacheProvider.__new__(CacheProvider)
    provider._cache = FakeCache()
    provider._ob_store = None
    prov = provider.get_provenance("m1", "polymarket")
    assert prov.source == "pmxt"
    assert prov.observed is True
