from __future__ import annotations

from typing import Protocol, runtime_checkable

from agenttrader.data.models import (
    DataProvenance,
    Market,
    OrderBook,
    PricePoint,
)


@runtime_checkable
class MarketDataProvider(Protocol):
    def get_markets(
        self,
        platform: str = "all",
        category: str | None = None,
        active_only: bool = False,
        limit: int = 1000,
    ) -> list[Market]: ...

    def get_price_history(
        self,
        market_id: str,
        platform: str,
        start_ts: int,
        end_ts: int,
    ) -> list[PricePoint]: ...

    def get_latest_price(
        self,
        market_id: str,
        platform: str,
    ) -> PricePoint | None: ...

    def get_orderbook(
        self,
        market_id: str,
        platform: str,
        timestamp: int,
    ) -> OrderBook | None: ...

    def get_provenance(
        self,
        market_id: str,
        platform: str,
    ) -> DataProvenance: ...
