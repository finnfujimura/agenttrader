from datetime import UTC, datetime
from pathlib import Path

from agenttrader.core.backtest_engine import BacktestConfig, BacktestEngine
from agenttrader.core.base_strategy import BaseStrategy
from agenttrader.data.backtest_artifacts import read_backtest_artifact, write_backtest_artifact
from agenttrader.data.models import Market, MarketType, Platform, PricePoint


def test_backtest_artifact_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("agenttrader.data.backtest_artifacts.ARTIFACTS_DIR", tmp_path)
    run_id = "run-artifact-test"
    curve = [{"timestamp": 1, "value": 100.0}]
    trades = [{"id": "t1", "action": "buy"}]
    path = write_backtest_artifact(run_id, curve, trades)
    assert Path(path).exists()
    loaded = read_backtest_artifact(run_id)
    assert loaded["equity_curve"] == curve
    assert loaded["trades"] == trades


def test_streaming_backtest_returns_summary_and_artifact_payload(monkeypatch):
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    market = Market(
        id="m1",
        condition_id="c1",
        platform=Platform.POLYMARKET,
        title="Test Market",
        category="politics",
        tags=["test"],
        market_type=MarketType.BINARY,
        volume=1000.0,
        close_time=now_ts + 86400,
        resolved=False,
        resolution=None,
        scalar_low=None,
        scalar_high=None,
    )

    class FakeParquet:
        def __init__(self, *args, **kwargs):
            return

        def is_available(self):
            return True

        def get_markets(self, platform="all", limit=50000):  # noqa: ARG002
            return [market]

    class FakeIndex:
        def get_market_ids(self, platform="all", start_ts=None, end_ts=None):  # noqa: ARG002
            return [("m1", "polymarket")]

        def get_market_ids_with_counts(self, platform="all", start_ts=None, end_ts=None):  # noqa: ARG002
            return [("m1", "polymarket", 2)]

        def stream_market_history(self, market_id, platform, start_ts, end_ts):  # noqa: ARG002
            yield PricePoint(timestamp=now_ts + 60, yes_price=0.45, no_price=0.55, volume=10.0)
            yield PricePoint(timestamp=now_ts + 120, yes_price=0.55, no_price=0.45, volume=12.0)

        def stream_market_history_resampled(self, market_id, platform, start_ts, end_ts, bar_seconds):  # noqa: ARG002
            yield from self.stream_market_history(market_id, platform, start_ts, end_ts)

        def get_latest_price_before(self, market_id, platform, ts):  # noqa: ARG002
            return 0.40

    class Strategy(BaseStrategy):
        def on_start(self):
            self.subscribe(platform="polymarket")

        def on_market_data(self, market, price, orderbook):  # noqa: ARG002
            if price < 0.50 and self.get_position(market.id) is None:
                self.buy(market.id, contracts=1)
            elif price > 0.50 and self.get_position(market.id) is not None:
                self.sell(market.id)

    monkeypatch.setattr("agenttrader.data.parquet_adapter.ParquetDataAdapter", FakeParquet)
    engine = BacktestEngine(data_source=None)
    result = engine._run_streaming(
        Strategy,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_cash=1000.0,
        ),
        FakeIndex(),
    )
    assert result["ok"] is True
    assert result["data_source"] == "normalized-index"
    assert result["markets_tested"] == 1
    assert result["fidelity"] == "exact_trade"
    assert result["max_markets_applied"] is None
    assert "metrics" in result
    assert "equity_curve" not in result
    assert "trades" not in result
    assert "_artifact_payload" in result
    assert len(result["_artifact_payload"]["equity_curve"]) > 0


def test_streaming_backtest_applies_guardrails(monkeypatch):
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    markets = [
        Market(
            id="m1",
            condition_id="c1",
            platform=Platform.POLYMARKET,
            title="Market 1",
            category="politics",
            tags=[],
            market_type=MarketType.BINARY,
            volume=1000.0,
            close_time=now_ts + 86400,
            resolved=False,
            resolution=None,
            scalar_low=None,
            scalar_high=None,
        ),
        Market(
            id="m2",
            condition_id="c2",
            platform=Platform.POLYMARKET,
            title="Market 2",
            category="politics",
            tags=[],
            market_type=MarketType.BINARY,
            volume=800.0,
            close_time=now_ts + 86400,
            resolved=False,
            resolution=None,
            scalar_low=None,
            scalar_high=None,
        ),
    ]

    class FakeParquet:
        def __init__(self, *args, **kwargs):
            return

        def is_available(self):
            return True

        def get_markets(self, platform="all", limit=50000):  # noqa: ARG002
            return markets

    class FakeIndex:
        def __init__(self):
            self.resampled_calls = 0

        def get_market_ids(self, platform="all", start_ts=None, end_ts=None):  # noqa: ARG002
            return [("m1", "polymarket"), ("m2", "polymarket")]

        def get_market_ids_with_counts(self, platform="all", start_ts=None, end_ts=None):  # noqa: ARG002
            return [("m1", "polymarket", 100), ("m2", "polymarket", 50)]

        def stream_market_history(self, market_id, platform, start_ts, end_ts):  # noqa: ARG002
            yield PricePoint(timestamp=now_ts + 60, yes_price=0.45, no_price=0.55, volume=10.0)

        def stream_market_history_resampled(self, market_id, platform, start_ts, end_ts, bar_seconds):  # noqa: ARG002
            self.resampled_calls += 1
            yield PricePoint(timestamp=now_ts + bar_seconds, yes_price=0.50, no_price=0.50, volume=20.0)

        def get_latest_price_before(self, market_id, platform, ts):  # noqa: ARG002
            return 0.40

    class Strategy(BaseStrategy):
        def on_start(self):
            self.subscribe(platform="polymarket")

        def on_market_data(self, market, price, orderbook):  # noqa: ARG002
            return

    monkeypatch.setattr("agenttrader.data.parquet_adapter.ParquetDataAdapter", FakeParquet)
    index = FakeIndex()
    engine = BacktestEngine(data_source=None)
    result = engine._run_streaming(
        Strategy,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_cash=1000.0,
            max_markets=1,
            fidelity="bar_1h",
        ),
        index,
    )
    assert result["ok"] is True
    assert result["fidelity"] == "bar_1h"
    assert result["max_markets_applied"] == 1
    assert result["markets_tested"] == 1
    assert index.resampled_calls >= 1
