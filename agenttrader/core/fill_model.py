# DO NOT import dome_api_sdk here. Use agenttrader.data.dome_client only.
from __future__ import annotations

from agenttrader.data.models import FillResult, OrderBook


class FillModel:
    def simulate_buy(
        self,
        contracts: float,
        orderbook: OrderBook,
        order_type: str = "market",
        limit_price: float | None = None,
    ) -> FillResult:
        if contracts <= 0:
            return FillResult(False, 0.0, 0.0, 0.0, False)
        if not orderbook.asks:
            return FillResult(False, 0.0, 0.0, 0.0, True)

        if order_type == "limit":
            best_ask = orderbook.best_ask
            if best_ask is None or limit_price is None or best_ask > limit_price:
                return FillResult(False, 0.0, 0.0, 0.0, False)
            filled_contracts = min(contracts, sum(level.size for level in orderbook.asks if level.price <= limit_price))
            if filled_contracts <= 0:
                return FillResult(False, 0.0, 0.0, 0.0, False)
            fill_price = float(limit_price)
            mid = orderbook.mid or fill_price
            return FillResult(
                filled=True,
                fill_price=fill_price,
                contracts=filled_contracts,
                slippage=fill_price - mid,
                partial=filled_contracts < contracts,
            )

        remaining = contracts
        total_cost = 0.0
        filled_contracts = 0.0
        for level in orderbook.asks:
            take = min(remaining, level.size)
            if take <= 0:
                continue
            total_cost += take * level.price
            filled_contracts += take
            remaining -= take
            if remaining <= 0:
                break

        if filled_contracts <= 0:
            return FillResult(False, 0.0, 0.0, 0.0, True)
        fill_price = total_cost / filled_contracts
        mid = orderbook.mid or fill_price
        return FillResult(
            filled=True,
            fill_price=fill_price,
            contracts=filled_contracts,
            slippage=fill_price - mid,
            partial=filled_contracts < contracts,
        )

    def simulate_sell(
        self,
        contracts: float,
        orderbook: OrderBook,
        order_type: str = "market",
        limit_price: float | None = None,
    ) -> FillResult:
        if contracts <= 0:
            return FillResult(False, 0.0, 0.0, 0.0, False)
        if not orderbook.bids:
            return FillResult(False, 0.0, 0.0, 0.0, True)

        if order_type == "limit":
            best_bid = orderbook.best_bid
            if best_bid is None or limit_price is None or best_bid < limit_price:
                return FillResult(False, 0.0, 0.0, 0.0, False)
            filled_contracts = min(contracts, sum(level.size for level in orderbook.bids if level.price >= limit_price))
            if filled_contracts <= 0:
                return FillResult(False, 0.0, 0.0, 0.0, False)
            fill_price = float(limit_price)
            mid = orderbook.mid or fill_price
            return FillResult(
                filled=True,
                fill_price=fill_price,
                contracts=filled_contracts,
                slippage=fill_price - mid,
                partial=filled_contracts < contracts,
            )

        remaining = contracts
        total_proceeds = 0.0
        filled_contracts = 0.0
        for level in orderbook.bids:
            take = min(remaining, level.size)
            if take <= 0:
                continue
            total_proceeds += take * level.price
            filled_contracts += take
            remaining -= take
            if remaining <= 0:
                break

        if filled_contracts <= 0:
            return FillResult(False, 0.0, 0.0, 0.0, True)
        fill_price = total_proceeds / filled_contracts
        mid = orderbook.mid or fill_price
        return FillResult(
            filled=True,
            fill_price=fill_price,
            contracts=filled_contracts,
            slippage=fill_price - mid,
            partial=filled_contracts < contracts,
        )
