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


def test_get_markets_falls_back_to_parquet_when_index_unavailable():
    provider = IndexProvider.__new__(IndexProvider)
    provider._index = FakeIndexUnavailable()
    provider._parquet = FakeParquetMarkets()

    markets = provider.get_markets(platform="polymarket", category="crypto", limit=5)

    assert len(markets) == 1
    assert markets[0].platform == Platform.POLYMARKET


class FakeIndexUnavailable:
    def is_available(self, require_market_catalog=False):  # noqa: ARG002
        return False


class FakeParquetMarkets:
    def get_markets(self, platform="all", category=None, active_only=False, limit=1000):  # noqa: ARG002
        return [
            Market(
                id="m1",
                condition_id="c1",
                platform=Platform.POLYMARKET,
                title="BTC",
                category=category or "crypto",
                tags=[],
                market_type=MarketType.BINARY,
                volume=10.0,
                close_time=0,
                resolved=False,
                resolution=None,
                scalar_low=None,
                scalar_high=None,
            )
        ]
