from datetime import UTC, datetime

import pytest

from agenttrader.core.backtest_engine import BacktestConfig, BacktestEngine
from agenttrader.core.base_strategy import BaseStrategy
from agenttrader.data.models import ExecutionMode, Market, MarketType, Platform, PricePoint


class BuyAndHoldStrategy(BaseStrategy):
    def on_start(self):
        self.subscribe(platform="polymarket")

    def on_market_data(self, market, price, orderbook):
        if self.get_position(market.id) is None:
            self.buy(market.id, contracts=10)


class OrderbookDependentStrategy(BaseStrategy):
    def on_start(self):
        self.subscribe(platform="polymarket")

    def on_market_data(self, market, price, orderbook):
        # This strategy explicitly accesses orderbook — should fail in strict mode
        if orderbook is None:
            return
        spread = orderbook.best_ask - orderbook.best_bid
        if spread < 0.02 and self.get_position(market.id) is None:
            self.buy(market.id, contracts=10)


def _make_market(now_ts):
    return Market(
        id="m1", condition_id="c1", platform=Platform.POLYMARKET,
        title="Test", category="politics", tags=[],
        market_type=MarketType.BINARY, volume=1000,
        close_time=now_ts + 86400, resolved=True, resolution="yes",
        scalar_low=None, scalar_high=None,
    )


def test_strict_backtest_buy_and_hold(monkeypatch):
    """Buy-and-hold works in strict mode — doesn't need orderbooks."""
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    market = _make_market(now_ts)

    class FakeParquet:
        def __init__(self, *args, **kwargs):
            return

        def is_available(self):
            return True

        def get_markets(self, platform="all", limit=50000):
            return [market]

        def get_markets_by_ids(self, market_ids, platform="all"):
            if "m1" in set(market_ids):
                return [market]
            return []

    class FakeIndex:
        def get_market_ids(self, platform="all", start_ts=None, end_ts=None):
            return [("m1", "polymarket")]

        def get_market_ids_with_counts(self, platform="all", start_ts=None, end_ts=None):
            return [("m1", "polymarket", 3)]

        def stream_market_history(self, market_id, platform, start_ts, end_ts):
            yield PricePoint(now_ts + 60, 0.45, 0.55, 10)
            yield PricePoint(now_ts + 120, 0.50, 0.50, 12)
            yield PricePoint(now_ts + 180, 0.55, 0.45, 15)

        def stream_market_history_resampled(self, *args, **kwargs):
            return self.stream_market_history(*args, **kwargs)

        def get_latest_price_before(self, market_id, platform, ts):
            return 0.40

    monkeypatch.setattr("agenttrader.data.parquet_adapter.ParquetDataAdapter", FakeParquet)
    engine = BacktestEngine()
    result = engine._run_streaming(
        BuyAndHoldStrategy,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-02",
            execution_mode=ExecutionMode.STRICT_PRICE_ONLY,
        ),
        FakeIndex(),
    )
    assert result["ok"] is True
    assert result["execution_mode"] == "strict_price_only"
    trades = result["_artifact_payload"]["trades"]
    buy_trades = [t for t in trades if t["action"] == "buy"]
    assert len(buy_trades) > 0
    for t in buy_trades:
        assert t["slippage"] == 0.0


def test_strict_backtest_orderbook_strategy_graceful(monkeypatch):
    """Strategy that checks orderbook gracefully handles None in strict mode."""
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    market = _make_market(now_ts)

    class FakeParquet:
        def __init__(self, *args, **kwargs):
            return

        def is_available(self):
            return True

        def get_markets(self, platform="all", limit=50000):
            return [market]

        def get_markets_by_ids(self, market_ids, platform="all"):
            if "m1" in set(market_ids):
                return [market]
            return []

    class FakeIndex:
        def get_market_ids(self, **kwargs):
            return [("m1", "polymarket")]

        def get_market_ids_with_counts(self, **kwargs):
            return [("m1", "polymarket", 1)]

        def stream_market_history(self, market_id, platform, start_ts, end_ts):
            yield PricePoint(now_ts + 60, 0.50, 0.50, 10)

        def stream_market_history_resampled(self, *args, **kwargs):
            return self.stream_market_history(*args, **kwargs)

        def get_latest_price_before(self, market_id, platform, ts):
            return 0.40

    monkeypatch.setattr("agenttrader.data.parquet_adapter.ParquetDataAdapter", FakeParquet)
    engine = BacktestEngine()
    # OrderbookDependentStrategy checks `if orderbook is None: return` — should run fine
    result = engine._run_streaming(
        OrderbookDependentStrategy,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-02",
            execution_mode=ExecutionMode.STRICT_PRICE_ONLY,
        ),
        FakeIndex(),
    )
    assert result["ok"] is True
    # No trades because strategy skips when orderbook is None
    trades = result["_artifact_payload"]["trades"]
    assert len(trades) == 0


def test_synthetic_mode_backtest_still_works(monkeypatch):
    """Synthetic mode continues to produce fills with synthetic OB slippage."""
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    market = _make_market(now_ts)

    class FakeParquet:
        def __init__(self, *args, **kwargs):
            return

        def is_available(self):
            return True

        def get_markets(self, platform="all", limit=50000):
            return [market]

        def get_markets_by_ids(self, market_ids, platform="all"):
            if "m1" in set(market_ids):
                return [market]
            return []

    class FakeIndex:
        def get_market_ids(self, **kwargs):
            return [("m1", "polymarket")]

        def get_market_ids_with_counts(self, **kwargs):
            return [("m1", "polymarket", 2)]

        def stream_market_history(self, market_id, platform, start_ts, end_ts):
            yield PricePoint(now_ts + 60, 0.45, 0.55, 10)
            yield PricePoint(now_ts + 120, 0.55, 0.45, 12)

        def stream_market_history_resampled(self, *args, **kwargs):
            return self.stream_market_history(*args, **kwargs)

        def get_latest_price_before(self, market_id, platform, ts):
            return 0.40

    monkeypatch.setattr("agenttrader.data.parquet_adapter.ParquetDataAdapter", FakeParquet)
    engine = BacktestEngine()
    result = engine._run_streaming(
        BuyAndHoldStrategy,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-02",
            execution_mode=ExecutionMode.SYNTHETIC_EXECUTION_MODEL,
        ),
        FakeIndex(),
    )
    assert result["ok"] is True
    assert result["execution_mode"] == "synthetic_execution_model"
    trades = result["_artifact_payload"]["trades"]
    buy_trades = [t for t in trades if t["action"] == "buy"]
    assert len(buy_trades) > 0
