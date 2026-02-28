from agenttrader.core.price_fill_model import PriceOnlyFillModel


def test_buy_fills_at_observed_price():
    model = PriceOnlyFillModel()
    result = model.fill_buy(contracts=10, observed_price=0.65)
    assert result.filled is True
    assert result.fill_price == 0.65
    assert result.contracts == 10
    assert result.slippage == 0.0
    assert result.partial is False


def test_sell_fills_at_observed_price():
    model = PriceOnlyFillModel()
    result = model.fill_sell(contracts=5, observed_price=0.70)
    assert result.filled is True
    assert result.fill_price == 0.70
    assert result.contracts == 5
    assert result.slippage == 0.0


def test_limit_buy_rejected_above_limit():
    model = PriceOnlyFillModel()
    result = model.fill_buy(contracts=10, observed_price=0.65, limit_price=0.60)
    assert result.filled is False


def test_limit_buy_accepted_at_limit():
    model = PriceOnlyFillModel()
    result = model.fill_buy(contracts=10, observed_price=0.55, limit_price=0.60)
    assert result.filled is True
    assert result.fill_price == 0.55


def test_limit_sell_rejected_below_limit():
    model = PriceOnlyFillModel()
    result = model.fill_sell(contracts=10, observed_price=0.40, limit_price=0.50)
    assert result.filled is False


def test_limit_sell_accepted_at_limit():
    model = PriceOnlyFillModel()
    result = model.fill_sell(contracts=10, observed_price=0.60, limit_price=0.50)
    assert result.filled is True
    assert result.fill_price == 0.60
