import pytest

from agenttrader.core.context import StreamingBacktestContext
from agenttrader.core.fill_model import FillModel
from agenttrader.data.models import (
    ExecutionMode, Market, MarketType, Platform, PricePoint,
)
from agenttrader.errors import AgentTraderError


def _make_context(mode: ExecutionMode) -> StreamingBacktestContext:
    market = Market(
        id="m1", condition_id="c1", platform=Platform.POLYMARKET,
        title="T", category="", tags=[], market_type=MarketType.BINARY,
        volume=100, close_time=0, resolved=False, resolution=None,
        scalar_low=None, scalar_high=None,
    )
    ctx = StreamingBacktestContext(
        initial_cash=1000.0,
        market_map={"m1": market},
        fill_model=FillModel(),
        execution_mode=mode,
    )
    ctx.advance_time(100)
    ctx.set_price_cursor("m1", 0.60)
    ctx.push_history("m1", PricePoint(100, 0.60, 0.40, 10))
    return ctx


def test_strict_get_orderbook_raises():
    ctx = _make_context(ExecutionMode.STRICT_PRICE_ONLY)
    with pytest.raises(AgentTraderError, match="NoObservedOrderbook"):
        ctx.get_orderbook("m1")


def test_strict_buy_fills_at_observed_price():
    ctx = _make_context(ExecutionMode.STRICT_PRICE_ONLY)
    trade_id = ctx.buy("m1", 10)
    assert trade_id
    trade = ctx._trades[-1]
    assert trade["price"] == 0.60
    assert trade["slippage"] == 0.0


def test_strict_sell_fills_at_observed_price():
    ctx = _make_context(ExecutionMode.STRICT_PRICE_ONLY)
    ctx.buy("m1", 10)
    trade_id = ctx.sell("m1")
    assert trade_id
    trade = ctx._trades[-1]
    assert trade["price"] == 0.60
    assert trade["slippage"] == 0.0


def test_synthetic_mode_still_synthesizes_orderbook():
    ctx = _make_context(ExecutionMode.SYNTHETIC_EXECUTION_MODEL)
    ob = ctx.get_orderbook("m1")
    assert ob is not None
    assert len(ob.asks) > 0


def test_compile_results_includes_execution_mode():
    ctx = _make_context(ExecutionMode.STRICT_PRICE_ONLY)
    results = ctx.compile_results()
    assert results["execution_mode"] == "strict_price_only"
