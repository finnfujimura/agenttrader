# DO NOT import dome_api_sdk here. Use agenttrader.data.dome_client only.
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from agenttrader.core.context import ExecutionContext
    from agenttrader.data.models import Market, OrderBook, Position, PricePoint


class BaseStrategy(ABC):
    """
    Subclass this to create a trading strategy.

    Strategies are plain .py files. The execution engine (backtester or
    paper trader) imports your class and calls the lifecycle hooks below.

    RULES:
    - Do NOT import dome_api_sdk or make network calls directly.
    - Do NOT import requests, httpx, or any networking library.
    - All data access must go through self.* methods.
    - All order placement must go through self.buy() and self.sell().
    """

    def __init__(self, context: "ExecutionContext"):
        self._ctx = context

    def on_start(self) -> None:
        """Called once when strategy initializes. Subscribe to markets here."""

    @abstractmethod
    def on_market_data(self, market: "Market", price: float, orderbook: "OrderBook") -> None:
        """
        Called on every price update for all subscribed markets.

        Args:
            market:    The market that updated. Check market.market_type
                       to know if it's BINARY, CATEGORICAL, or SCALAR.
            price:     Current YES price (0.0 to 1.0).
            orderbook: Current orderbook snapshot. Use for slippage estimates.
        """

    def on_resolution(self, market: "Market", outcome: str, pnl: float) -> None:
        """
        Called when a subscribed market resolves.

        Args:
            market:  The resolved market.
            outcome: 'yes' | 'no' for BINARY. Winning option name for
                     CATEGORICAL. Numeric string for SCALAR.
            pnl:     Realized P&L from this resolution in dollars.
        """

    def on_schedule(self, now: datetime, market: "Market") -> None:
        """
        Called every 15 minutes (configurable) for each subscribed market.
        Use for time-decay strategies and pre-expiry position management.
        """

    def on_stop(self) -> None:
        """Called on graceful shutdown."""

    def subscribe(
        self,
        platform: str = "all",
        category: Optional[str] = None,
        tags: Optional[list[str]] = None,
        market_ids: Optional[list[str]] = None,
    ) -> None:
        self._ctx.subscribe(platform, category, tags, market_ids)

    def search_markets(self, query: str, platform: str = "all") -> list["Market"]:
        return self._ctx.search_markets(query, platform)

    def get_price(self, market_id: str) -> float:
        return self._ctx.get_price(market_id)

    def get_orderbook(self, market_id: str) -> "OrderBook":
        return self._ctx.get_orderbook(market_id)

    def get_history(self, market_id: str, lookback_hours: int = 24) -> list["PricePoint"]:
        return self._ctx.get_history(market_id, lookback_hours)

    def get_position(self, market_id: str) -> Optional["Position"]:
        return self._ctx.get_position(market_id)

    def get_cash(self) -> float:
        return self._ctx.get_cash()

    def get_portfolio_value(self) -> float:
        return self._ctx.get_portfolio_value()

    def buy(
        self,
        market_id: str,
        contracts: float,
        side: str = "yes",
        order_type: str = "market",
        limit_price: Optional[float] = None,
    ) -> str:
        return self._ctx.buy(market_id, contracts, side, order_type, limit_price)

    def sell(self, market_id: str, contracts: Optional[float] = None) -> str:
        return self._ctx.sell(market_id, contracts)

    def log(self, message: str) -> None:
        self._ctx.log(message)

    def set_state(self, key: str, value) -> None:
        self._ctx.set_state(key, value)

    def get_state(self, key: str, default=None):
        return self._ctx.get_state(key, default)
