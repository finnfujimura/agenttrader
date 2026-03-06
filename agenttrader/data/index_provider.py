"""MarketDataProvider backed by parquet dataset + DuckDB normalized index."""
from __future__ import annotations

from agenttrader.data.models import (
    DataProvenance,
    Market,
    OrderBook,
    PricePoint,
)


class IndexProvider:
    """Wraps BacktestIndexAdapter + ParquetDataAdapter behind MarketDataProvider."""

    def __init__(self):
        from agenttrader.data.index_adapter import BacktestIndexAdapter

        self._index = BacktestIndexAdapter()
        self._parquet = None

    def is_available(self) -> bool:
        return self._index.is_available(require_market_catalog=True)

    def _get_parquet_fallback(self):
        if self._parquet is None:
            from agenttrader.data.parquet_adapter import ParquetDataAdapter

            self._parquet = ParquetDataAdapter()
        return self._parquet

    def _index_available(self) -> bool:
        index = getattr(self, "_index", None)
        if index is None:
            return False
        return bool(index.is_available(require_market_catalog=True))

    def close(self) -> None:
        if getattr(self, "_index", None) is not None:
            self._index.close()
        if getattr(self, "_parquet", None) is not None:
            self._parquet.close()

    def get_markets(self, platform="all", category=None, active_only=False, limit=1000) -> list[Market]:
        kwargs = {
            "platform": platform,
            "category": category,
            "limit": limit,
        }
        if active_only:
            kwargs["active_only"] = True
        if self._index_available():
            return self._index.get_markets(**kwargs)
        return self._get_parquet_fallback().get_markets(**kwargs)

    def get_markets_by_ids(self, market_ids: list[str], platform: str = "all") -> list[Market]:
        if self._index_available():
            return self._index.get_markets_by_ids(market_ids=market_ids, platform=platform)
        return self._get_parquet_fallback().get_markets_by_ids(market_ids=market_ids, platform=platform)

    def get_markets_by_ids_bulk(self, market_ids: list[str], platform: str = "all") -> list[Market]:
        if self._index_available():
            return self._index.get_markets_by_ids_bulk(market_ids=market_ids, platform=platform)
        return self._get_parquet_fallback().get_markets_by_ids_bulk(market_ids=market_ids, platform=platform)

    def get_price_history(self, market_id, platform, start_ts, end_ts) -> list[PricePoint]:
        if self._index.is_available():
            return self._index.get_price_history(market_id, platform, start_ts, end_ts)
        return self._get_parquet_fallback().get_price_history(market_id, platform, start_ts, end_ts)

    def get_latest_price(self, market_id, platform) -> PricePoint | None:
        if self._index.is_available():
            return self._index.get_latest_price(market_id, platform)
        history = self._get_parquet_fallback().get_price_history(market_id, platform, 0, 2**31)
        return history[-1] if history else None

    def get_orderbook(self, market_id, platform, timestamp) -> OrderBook | None:
        return None

    def get_provenance(self, market_id, platform) -> DataProvenance:
        return DataProvenance(source="index", observed=True, granularity="trade")

    # Streaming delegation (used by BacktestEngine)
    def stream_market_history(self, market_id, platform, start_ts, end_ts):
        return self._index.stream_market_history(market_id, platform, start_ts, end_ts)

    def stream_market_history_resampled(self, market_id, platform, start_ts, end_ts, bar_seconds):
        return self._index.stream_market_history_resampled(market_id, platform, start_ts, end_ts, bar_seconds)

    def get_market_ids(self, platform, start_ts, end_ts):
        return self._index.get_market_ids(platform=platform, start_ts=start_ts, end_ts=end_ts)

    def get_market_ids_with_counts(self, platform, start_ts, end_ts):
        return self._index.get_market_ids_with_counts(platform=platform, start_ts=start_ts, end_ts=end_ts)

    def get_latest_price_before(self, market_id, platform, ts):
        return self._index.get_latest_price_before(market_id, platform, ts)
