"""Verify that no code path silently generates synthetic orderbooks."""
import pytest
from unittest.mock import MagicMock
from agenttrader.core.context import BacktestContext, StreamingBacktestContext, LiveContext
from agenttrader.core.fill_model import FillModel
from agenttrader.data.models import (
    ExecutionMode, Market, MarketType, Platform, PricePoint,
)
from agenttrader.errors import AgentTraderError


def _market():
    return Market(
        id="m1", condition_id="c1", platform=Platform.POLYMARKET,
        title="T", category="", tags=[], market_type=MarketType.BINARY,
        volume=100, close_time=0, resolved=False, resolution=None,
        scalar_low=None, scalar_high=None,
    )


def test_streaming_strict_no_synthetic():
    ctx = StreamingBacktestContext(1000, {"m1": _market()}, FillModel(),
                                   execution_mode=ExecutionMode.STRICT_PRICE_ONLY)
    ctx.advance_time(100)
    ctx.set_price_cursor("m1", 0.50)
    with pytest.raises(AgentTraderError, match="NoObservedOrderbook"):
        ctx.get_orderbook("m1")


def test_streaming_observed_no_synthetic():
    ctx = StreamingBacktestContext(1000, {"m1": _market()}, FillModel(),
                                   execution_mode=ExecutionMode.OBSERVED_ORDERBOOK)
    ctx.advance_time(100)
    ctx.set_price_cursor("m1", 0.50)
    with pytest.raises(AgentTraderError, match="NoObservedOrderbook"):
        ctx.get_orderbook("m1")


def test_legacy_strict_no_synthetic():
    ctx = BacktestContext(
        1000, {"m1": [PricePoint(100, 0.50, 0.50, 10)]},
        None, {"m1": _market()},
        execution_mode=ExecutionMode.STRICT_PRICE_ONLY,
    )
    ctx.advance_time(100)
    with pytest.raises(AgentTraderError, match="NoObservedOrderbook"):
        ctx.get_orderbook("m1")


def test_legacy_observed_no_synthetic():
    ctx = BacktestContext(
        1000, {"m1": [PricePoint(100, 0.50, 0.50, 10)]},
        None, {"m1": _market()},
        execution_mode=ExecutionMode.OBSERVED_ORDERBOOK,
    )
    ctx.advance_time(100)
    with pytest.raises(AgentTraderError, match="NoObservedOrderbook"):
        ctx.get_orderbook("m1")


def test_live_no_synthetic():
    cache = MagicMock()
    cache.get_market.return_value = _market()
    ob_store = MagicMock()
    ob_store.get_nearest.return_value = None
    ctx = LiveContext("p1", 1000, cache, ob_store)
    ctx._current_prices["m1"] = 0.50
    with pytest.raises(AgentTraderError, match="NoObservedOrderbook"):
        ctx.get_orderbook("m1")


def test_streaming_synthetic_mode_still_works():
    """Synthetic mode is the only path that should produce OBs."""
    ctx = StreamingBacktestContext(1000, {"m1": _market()}, FillModel(),
                                   execution_mode=ExecutionMode.SYNTHETIC_EXECUTION_MODEL)
    ctx.advance_time(100)
    ctx.set_price_cursor("m1", 0.50)
    ob = ctx.get_orderbook("m1")
    assert ob is not None
    assert len(ob.asks) > 0
    assert len(ob.bids) > 0
