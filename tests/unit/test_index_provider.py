from agenttrader.data.index_provider import IndexProvider
from agenttrader.data.models import Market, MarketType, Platform, PricePoint
from agenttrader.data.provider import MarketDataProvider


def test_get_orderbook_returns_none():
    """Historical parquet data has no observed orderbooks."""
    class FakeParquet:
        def is_available(self):
            return True
        def get_markets(self, platform="all", limit=50000):
            return []

    class FakeIndex:
        def is_available(self):
            return True

    provider = IndexProvider.__new__(IndexProvider)
    provider._parquet = FakeParquet()
    provider._index = FakeIndex()
    result = provider.get_orderbook("m1", "polymarket", 0)
    assert result is None


def test_provenance_reports_index_observed():
    class FakeParquet:
        def is_available(self):
            return True
    class FakeIndex:
        def is_available(self):
            return True

    provider = IndexProvider.__new__(IndexProvider)
    provider._parquet = FakeParquet()
    provider._index = FakeIndex()
    prov = provider.get_provenance("m1", "polymarket")
    assert prov.source == "index"
    assert prov.observed is True
    assert prov.granularity == "trade"
