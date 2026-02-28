"""Price-only fill model for strict_price_only execution mode.

Fills at the current observed price with zero slippage.
No orderbook is consulted. This is the honest default: we report
"filled at observed price" without modeling spread or depth.
"""
from __future__ import annotations

from agenttrader.data.models import FillResult


class PriceOnlyFillModel:
    def fill_buy(
        self,
        contracts: float,
        observed_price: float,
        limit_price: float | None = None,
    ) -> FillResult:
        if contracts <= 0:
            return FillResult(False, 0.0, 0.0, 0.0, False)
        if limit_price is not None and observed_price > limit_price:
            return FillResult(False, 0.0, 0.0, 0.0, False)
        return FillResult(
            filled=True,
            fill_price=observed_price,
            contracts=contracts,
            slippage=0.0,
            partial=False,
        )

    def fill_sell(
        self,
        contracts: float,
        observed_price: float,
        limit_price: float | None = None,
    ) -> FillResult:
        if contracts <= 0:
            return FillResult(False, 0.0, 0.0, 0.0, False)
        if limit_price is not None and observed_price < limit_price:
            return FillResult(False, 0.0, 0.0, 0.0, False)
        return FillResult(
            filled=True,
            fill_price=observed_price,
            contracts=contracts,
            slippage=0.0,
            partial=False,
        )
