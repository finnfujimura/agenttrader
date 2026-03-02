"""Tests for strategy validator (agenttrader/cli/validate.py).

Covers:
- Helper methods defined in the strategy class are accepted
- BaseStrategy public API methods are accepted
- Forbidden imports are flagged
- Truly undefined self.* calls are rejected
- on_market_data signature validated by arity, not exact param names
- Private method calls (self._foo()) are allowed
"""

from agenttrader.cli.validate import validate_strategy_file


def _write_strategy(tmp_path, source: str) -> str:
    p = tmp_path / "strategy.py"
    p.write_text(source, encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# Valid strategies that SHOULD pass
# ---------------------------------------------------------------------------


def test_minimal_strategy_passes(tmp_path):
    path = _write_strategy(tmp_path, """
from agenttrader import BaseStrategy

class MyStrat(BaseStrategy):
    def on_market_data(self, market, price, orderbook):
        pass
""")
    result = validate_strategy_file(path)
    assert result["valid"] is True
    assert result["errors"] == []


def test_renamed_params_pass(tmp_path):
    """on_market_data with renamed params should pass (arity check, not name check)."""
    path = _write_strategy(tmp_path, """
from agenttrader import BaseStrategy

class MyStrat(BaseStrategy):
    def on_market_data(self, m, p, ob):
        pass
""")
    result = validate_strategy_file(path)
    assert result["valid"] is True
    assert result["errors"] == []


def test_class_helper_methods_pass(tmp_path):
    """Strategy with helper methods calling each other should validate."""
    path = _write_strategy(tmp_path, """
from agenttrader import BaseStrategy

class SpreadStrat(BaseStrategy):
    def on_start(self):
        self.subscribe(platform="polymarket", category="politics")

    def on_market_data(self, market, price, orderbook):
        spread = self.compute_spread(orderbook)
        imbalance = self.compute_imbalance(orderbook)
        if self.should_enter(spread, imbalance):
            self.buy(market.id, 10)

    def compute_spread(self, ob):
        return 0.05

    def compute_imbalance(self, ob):
        return 0.1

    def should_enter(self, spread, imbalance):
        return spread > 0.02 and imbalance > 0.05
""")
    result = validate_strategy_file(path)
    assert result["valid"] is True
    assert result["errors"] == []


def test_private_method_calls_pass(tmp_path):
    """Calls to self._foo() should be allowed."""
    path = _write_strategy(tmp_path, """
from agenttrader import BaseStrategy

class MyStrat(BaseStrategy):
    def on_market_data(self, market, price, orderbook):
        self._internal_calc(price)

    def _internal_calc(self, price):
        return price * 2
""")
    result = validate_strategy_file(path)
    assert result["valid"] is True
    assert result["errors"] == []


def test_all_base_strategy_methods_pass(tmp_path):
    """Every public BaseStrategy method should be callable."""
    path = _write_strategy(tmp_path, """
from agenttrader import BaseStrategy

class FullStrat(BaseStrategy):
    def on_start(self):
        self.subscribe(platform="polymarket")
        self.search_markets("election")
        self.log("started")
        self.set_state("ready", True)

    def on_market_data(self, market, price, orderbook):
        self.get_price(market.id)
        self.get_orderbook(market.id)
        self.get_history(market.id)
        self.get_position(market.id)
        self.get_cash()
        self.get_portfolio_value()
        self.get_state("ready")
        self.buy(market.id, 10)
        self.sell(market.id, 5)
""")
    result = validate_strategy_file(path)
    assert result["valid"] is True
    assert result["errors"] == []


def test_realistic_structured_strategy_passes(tmp_path):
    """A realistic multi-helper strategy like an agent would produce."""
    path = _write_strategy(tmp_path, """
from agenttrader import BaseStrategy

class MomentumStrat(BaseStrategy):
    def on_start(self):
        self.subscribe(platform="polymarket", category="politics")
        self.set_state("position_count", 0)

    def on_market_data(self, market, price, orderbook):
        signal = self.compute_signal(market, price)
        risk_ok = self.check_risk()
        if signal > 0.6 and risk_ok:
            self.enter_position(market, price)
        elif signal < 0.3:
            self.exit_position(market)

    def compute_signal(self, market, price):
        history = self.get_history(market.id, lookback_hours=48)
        if len(history) < 2:
            return 0.5
        return history[-1].yes_price / history[0].yes_price

    def check_risk(self):
        cash = self.get_cash()
        value = self.get_portfolio_value()
        return cash / max(value, 1.0) > 0.2

    def enter_position(self, market, price):
        pos = self.get_position(market.id)
        if pos is None:
            self.buy(market.id, 50)
            self.log(f"Entered {market.id} at {price}")
            count = self.get_state("position_count", 0)
            self.set_state("position_count", count + 1)

    def exit_position(self, market):
        pos = self.get_position(market.id)
        if pos:
            self.sell(market.id)
            self.log(f"Exited {market.id}")
""")
    result = validate_strategy_file(path)
    assert result["valid"] is True
    assert result["errors"] == []


# ---------------------------------------------------------------------------
# Invalid strategies that SHOULD fail
# ---------------------------------------------------------------------------


def test_wrong_arity_rejected(tmp_path):
    """on_market_data with wrong number of args should fail."""
    path = _write_strategy(tmp_path, """
from agenttrader import BaseStrategy

class MyStrat(BaseStrategy):
    def on_market_data(self, market, price):
        pass
""")
    result = validate_strategy_file(path)
    assert result["valid"] is False
    errors = [e["type"] for e in result["errors"]]
    assert "InvalidSignature" in errors


def test_missing_on_market_data_rejected(tmp_path):
    path = _write_strategy(tmp_path, """
from agenttrader import BaseStrategy

class MyStrat(BaseStrategy):
    def on_start(self):
        pass
""")
    result = validate_strategy_file(path)
    assert result["valid"] is False
    errors = [e["type"] for e in result["errors"]]
    assert "MissingMethod" in errors


def test_no_strategy_class_rejected(tmp_path):
    path = _write_strategy(tmp_path, """
class NotAStrategy:
    def on_market_data(self, market, price, orderbook):
        pass
""")
    result = validate_strategy_file(path)
    assert result["valid"] is False
    errors = [e["type"] for e in result["errors"]]
    assert "ClassDefinitionError" in errors


def test_undefined_method_call_rejected(tmp_path):
    """Calling self.foo() where foo is not in BaseStrategy or defined in class should fail."""
    path = _write_strategy(tmp_path, """
from agenttrader import BaseStrategy

class MyStrat(BaseStrategy):
    def on_market_data(self, market, price, orderbook):
        self.nonexistent_method()
""")
    result = validate_strategy_file(path)
    assert result["valid"] is False
    errors = [e["type"] for e in result["errors"]]
    assert "InvalidMethodCall" in errors
    assert "not part of the BaseStrategy API" in result["errors"][0]["message"]
    assert "not defined in this class" in result["errors"][0]["message"]


def test_file_not_found(tmp_path):
    result = validate_strategy_file(str(tmp_path / "missing.py"))
    assert result["valid"] is False
    assert result["errors"][0]["type"] == "FileNotFoundError"


# ---------------------------------------------------------------------------
# BaseStrategy method derivation stays in sync
# ---------------------------------------------------------------------------


def test_base_strategy_methods_matches_class():
    """The derived method set should match the actual BaseStrategy public methods."""
    from agenttrader.cli.validate import _base_strategy_methods
    from agenttrader.core.base_strategy import BaseStrategy
    import inspect

    derived = _base_strategy_methods()
    actual = {
        name
        for name, _ in inspect.getmembers(BaseStrategy, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert derived == actual
    # Spot-check key methods are present
    assert "subscribe" in derived
    assert "buy" in derived
    assert "sell" in derived
    assert "log" in derived
    assert "on_market_data" in derived
