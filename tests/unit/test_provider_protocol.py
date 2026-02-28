from agenttrader.data.provider import MarketDataProvider
from agenttrader.data.models import (
    ExecutionMode, Market, MarketType, OrderBook, OrderLevel,
    Platform, PricePoint, DataProvenance,
)


class FakeProvider:
    """Minimal implementation that satisfies the protocol."""
    def get_markets(self, platform="all", category=None, limit=1000):
        return []

    def get_price_history(self, market_id, platform, start_ts, end_ts):
        return []

    def get_latest_price(self, market_id, platform):
        return None

    def get_orderbook(self, market_id, platform, timestamp):
        return None

    def get_provenance(self, market_id, platform):
        return DataProvenance(source="test", observed=True, granularity="trade")


def test_fake_satisfies_protocol():
    provider: MarketDataProvider = FakeProvider()
    assert provider.get_markets() == []
    assert provider.get_price_history("m1", "polymarket", 0, 1) == []
    assert provider.get_latest_price("m1", "polymarket") is None
    assert provider.get_orderbook("m1", "polymarket", 0) is None
    prov = provider.get_provenance("m1", "polymarket")
    assert prov.observed is True
