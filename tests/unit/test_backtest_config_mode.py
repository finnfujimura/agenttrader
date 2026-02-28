from agenttrader.core.backtest_engine import BacktestConfig
from agenttrader.data.models import ExecutionMode


def test_default_execution_mode_is_strict():
    cfg = BacktestConfig(strategy_path="s.py", start_date="2024-01-01", end_date="2024-01-02")
    assert cfg.execution_mode == ExecutionMode.STRICT_PRICE_ONLY


def test_can_set_synthetic_mode():
    cfg = BacktestConfig(
        strategy_path="s.py",
        start_date="2024-01-01",
        end_date="2024-01-02",
        execution_mode=ExecutionMode.SYNTHETIC_EXECUTION_MODEL,
    )
    assert cfg.execution_mode == ExecutionMode.SYNTHETIC_EXECUTION_MODEL
