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
        from agenttrader.data.parquet_adapter import ParquetDataAdapter

        self._index = BacktestIndexAdapter()
        self._parquet = ParquetDataAdapter()

    def is_available(self) -> bool:
        return self._index.is_available() and self._parquet.is_available()

    def close(self) -> None:
        self._index.close()

    def get_markets(self, platform="all", category=None, limit=1000) -> list[Market]:
        return self._parquet.get_markets(platform=platform, category=category, limit=limit)

    def get_price_history(self, market_id, platform, start_ts, end_ts) -> list[PricePoint]:
        return self._parquet.get_price_history(market_id, platform, start_ts, end_ts)

    def get_latest_price(self, market_id, platform) -> PricePoint | None:
        history = self._parquet.get_price_history(market_id, platform, 0, 2**31)
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
