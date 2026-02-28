"""MarketDataProvider backed by SQLite cache + file-based OrderBookStore."""
from __future__ import annotations

from agenttrader.data.models import (
    DataProvenance,
    Market,
    OrderBook,
    PricePoint,
)


class CacheProvider:
    """Wraps DataCache + OrderBookStore behind MarketDataProvider."""

    def __init__(self, cache, ob_store):
        self._cache = cache
        self._ob_store = ob_store

    def get_markets(self, platform="all", category=None, limit=1000) -> list[Market]:
        return self._cache.get_markets(platform=platform, category=category, limit=limit)

    def get_price_history(self, market_id, platform, start_ts, end_ts) -> list[PricePoint]:
        return self._cache.get_price_history(market_id, start_ts, end_ts)

    def get_latest_price(self, market_id, platform) -> PricePoint | None:
        return self._cache.get_latest_price(market_id)

    def get_orderbook(self, market_id, platform, timestamp) -> OrderBook | None:
        return self._ob_store.get_nearest(platform, market_id, timestamp)

    def get_provenance(self, market_id, platform) -> DataProvenance:
        return DataProvenance(source="pmxt", observed=True, granularity="1h")
