from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Platform(str, Enum):
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"


class MarketType(str, Enum):
    BINARY = "binary"
    CATEGORICAL = "categorical"
    SCALAR = "scalar"


@dataclass
class Market:
    id: str
    condition_id: str
    platform: Platform
    title: str
    category: str
    tags: list[str]
    market_type: MarketType
    volume: float
    close_time: int
    resolved: bool
    resolution: Optional[str]
    scalar_low: Optional[float]
    scalar_high: Optional[float]


@dataclass
class PricePoint:
    timestamp: int
    yes_price: float
    no_price: Optional[float]
    volume: float


@dataclass
class OrderLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    market_id: str
    timestamp: int
    bids: list[OrderLevel]
    asks: list[OrderLevel]

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None


@dataclass
class Position:
    id: str
    market_id: str
    platform: Platform
    side: str
    contracts: float
    avg_cost: float
    opened_at: int


@dataclass
class FillResult:
    filled: bool
    fill_price: float
    contracts: float
    slippage: float
    partial: bool
