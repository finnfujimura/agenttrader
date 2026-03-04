from datetime import UTC, datetime
from pathlib import Path

from agenttrader.core.backtest_engine import BacktestConfig, BacktestEngine
from agenttrader.core.base_strategy import BaseStrategy
from agenttrader.data.backtest_artifacts import read_backtest_artifact, write_backtest_artifact
from agenttrader.data.models import ExecutionMode, Market, MarketType, Platform, PricePoint


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

        def get_markets_by_ids(self, market_ids, platform="all"):  # noqa: ARG002
            if "m1" in set(market_ids):
                return [market]
            return []

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
            execution_mode=ExecutionMode.SYNTHETIC_EXECUTION_MODEL,
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


def test_streaming_backtest_hydrates_missing_exact_id_metadata(monkeypatch):
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    low_volume_market = Market(
        id="KXELONMARS-99",
        condition_id="KXELONMARS-99",
        platform=Platform.KALSHI,
        title="Will Elon visit Mars?",
        category="world",
        tags=["World", "International", "kxelonmars"],
        market_type=MarketType.BINARY,
        volume=5.0,
        close_time=now_ts + 86400,
        resolved=False,
        resolution=None,
        scalar_low=None,
        scalar_high=None,
    )
    high_volume_market = Market(
        id="KXTRUMPOUT-26-TRUMP",
        condition_id="KXTRUMPOUT-26-TRUMP",
        platform=Platform.KALSHI,
        title="Will Trump drop out?",
        category="politics",
        tags=["Politics"],
        market_type=MarketType.BINARY,
        volume=10_000.0,
        close_time=now_ts + 86400,
        resolved=False,
        resolution=None,
        scalar_low=None,
        scalar_high=None,
    )
    calls = {"get_markets_by_ids": 0}

    class FakeParquet:
        def __init__(self, *args, **kwargs):
            return

        def is_available(self):
            return True

        def get_markets(self, platform="all", limit=50000):  # noqa: ARG002
            return [high_volume_market]

        def get_markets_by_ids(self, market_ids, platform="all"):  # noqa: ARG002
            calls["get_markets_by_ids"] += 1
            if "KXELONMARS-99" in set(market_ids):
                return [low_volume_market]
            return []

    class FakeIndex:
        def get_market_ids(self, platform="all", start_ts=None, end_ts=None):  # noqa: ARG002
            return [("KXELONMARS-99", "kalshi")]

        def get_market_ids_with_counts(self, platform="all", start_ts=None, end_ts=None):  # noqa: ARG002
            return [("KXELONMARS-99", "kalshi", 1)]

        def stream_market_history(self, market_id, platform, start_ts, end_ts):  # noqa: ARG002
            yield PricePoint(timestamp=now_ts + 60, yes_price=0.55, no_price=0.45, volume=10.0)

        def stream_market_history_resampled(self, market_id, platform, start_ts, end_ts, bar_seconds):  # noqa: ARG002
            yield from self.stream_market_history(market_id, platform, start_ts, end_ts)

        def get_latest_price_before(self, market_id, platform, ts):  # noqa: ARG002
            return 0.50

    class Strategy(BaseStrategy):
        def on_start(self):
            self.subscribe(platform="kalshi", market_ids=["KXELONMARS-99"])

        def on_market_data(self, market, price, orderbook):  # noqa: ARG002
            return

    monkeypatch.setattr("agenttrader.data.parquet_adapter.ParquetDataAdapter", FakeParquet)
    engine = BacktestEngine(data_source=None)
    result = engine._run_streaming(
        Strategy,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_cash=1000.0,
            execution_mode=ExecutionMode.SYNTHETIC_EXECUTION_MODEL,
        ),
        FakeIndex(),
    )

    assert result["ok"] is True
    assert result["markets_tested"] == 1
    assert calls["get_markets_by_ids"] == 1


def test_streaming_backtest_broad_platform_subscription_uses_full_candidate_set(monkeypatch):
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    markets = {
        "poly-1": Market(
            id="poly-1",
            condition_id="poly-1",
            platform=Platform.POLYMARKET,
            title="Poly 1",
            category="politics",
            tags=["featured"],
            market_type=MarketType.BINARY,
            volume=100.0,
            close_time=now_ts + 86400,
            resolved=False,
            resolution=None,
            scalar_low=None,
            scalar_high=None,
        ),
        "poly-2": Market(
            id="poly-2",
            condition_id="poly-2",
            platform=Platform.POLYMARKET,
            title="Poly 2",
            category="sports",
            tags=[],
            market_type=MarketType.BINARY,
            volume=90.0,
            close_time=now_ts + 86400,
            resolved=False,
            resolution=None,
            scalar_low=None,
            scalar_high=None,
        ),
        "poly-3": Market(
            id="poly-3",
            condition_id="poly-3",
            platform=Platform.POLYMARKET,
            title="Poly 3",
            category="politics",
            tags=[],
            market_type=MarketType.BINARY,
            volume=80.0,
            close_time=now_ts + 86400,
            resolved=False,
            resolution=None,
            scalar_low=None,
            scalar_high=None,
        ),
        "kalshi-1": Market(
            id="kalshi-1",
            condition_id="kalshi-1",
            platform=Platform.KALSHI,
            title="Kalshi 1",
            category="politics",
            tags=[],
            market_type=MarketType.BINARY,
            volume=70.0,
            close_time=now_ts + 86400,
            resolved=False,
            resolution=None,
            scalar_low=None,
            scalar_high=None,
        ),
    }
    calls = {"get_markets_by_ids": 0}

    class FakeParquet:
        def __init__(self, *args, **kwargs):
            return

        def is_available(self):
            return True

        def get_markets(self, *args, **kwargs):  # noqa: ARG002
            raise AssertionError("Streaming broad subscription should not use capped get_markets() discovery")

        def get_markets_by_ids(self, market_ids, platform="all"):  # noqa: ARG002
            calls["get_markets_by_ids"] += 1
            return [markets[mid] for mid in market_ids if mid in markets]

    class FakeIndex:
        def get_market_ids_with_counts(self, platform="all", start_ts=None, end_ts=None):  # noqa: ARG002
            return [
                ("poly-1", "polymarket", 5),
                ("poly-2", "polymarket", 4),
                ("poly-3", "polymarket", 3),
                ("kalshi-1", "kalshi", 2),
            ]

        def stream_market_history(self, market_id, platform, start_ts, end_ts):  # noqa: ARG002
            yield PricePoint(timestamp=now_ts + 60, yes_price=0.55, no_price=0.45, volume=10.0)

        def stream_market_history_resampled(self, market_id, platform, start_ts, end_ts, bar_seconds):  # noqa: ARG002
            yield from self.stream_market_history(market_id, platform, start_ts, end_ts)

        def get_latest_price_before(self, market_id, platform, ts):  # noqa: ARG002
            return 0.50

    class Strategy(BaseStrategy):
        def on_start(self):
            self.subscribe(platform="polymarket")

        def on_market_data(self, market, price, orderbook):  # noqa: ARG002
            return

    monkeypatch.setattr("agenttrader.data.parquet_adapter.ParquetDataAdapter", FakeParquet)
    engine = BacktestEngine(data_source=None)
    result = engine._run_streaming(
        Strategy,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_cash=1000.0,
            execution_mode=ExecutionMode.SYNTHETIC_EXECUTION_MODEL,
        ),
        FakeIndex(),
    )

    assert result["ok"] is True
    assert result["markets_tested"] == 3
    assert calls["get_markets_by_ids"] >= 1


def test_streaming_backtest_broad_platform_prefers_bulk_metadata_loader(monkeypatch):
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    markets = {
        "poly-1": Market(
            id="poly-1",
            condition_id="poly-1",
            platform=Platform.POLYMARKET,
            title="Poly 1",
            category="politics",
            tags=[],
            market_type=MarketType.BINARY,
            volume=100.0,
            close_time=now_ts + 86400,
            resolved=False,
            resolution=None,
            scalar_low=None,
            scalar_high=None,
        ),
        "poly-2": Market(
            id="poly-2",
            condition_id="poly-2",
            platform=Platform.POLYMARKET,
            title="Poly 2",
            category="sports",
            tags=[],
            market_type=MarketType.BINARY,
            volume=90.0,
            close_time=now_ts + 86400,
            resolved=False,
            resolution=None,
            scalar_low=None,
            scalar_high=None,
        ),
    }
    calls = {"bulk": 0}

    class FakeParquet:
        def __init__(self, *args, **kwargs):
            return

        def is_available(self):
            return True

        def get_markets(self, *args, **kwargs):  # noqa: ARG002
            raise AssertionError("Streaming broad subscription should not use capped get_markets() discovery")

        def get_markets_by_ids(self, market_ids, platform="all"):  # noqa: ARG002
            raise AssertionError("Bulk metadata loader should be preferred for this path")

        def get_markets_by_ids_bulk(self, market_ids, platform="all"):  # noqa: ARG002
            calls["bulk"] += 1
            return [markets[mid] for mid in market_ids if mid in markets]

    class FakeIndex:
        def get_market_rows(self, platform="all", start_ts=None, end_ts=None):  # noqa: ARG002
            return [
                ("poly-1", "polymarket", 5, now_ts + 60, now_ts + 120),
                ("poly-2", "polymarket", 4, now_ts + 60, now_ts + 120),
            ]

        def stream_market_history(self, market_id, platform, start_ts, end_ts):  # noqa: ARG002
            yield PricePoint(timestamp=now_ts + 60, yes_price=0.55, no_price=0.45, volume=10.0)

        def stream_market_history_resampled(self, market_id, platform, start_ts, end_ts, bar_seconds):  # noqa: ARG002
            yield from self.stream_market_history(market_id, platform, start_ts, end_ts)

        def get_latest_price_before(self, market_id, platform, ts):  # noqa: ARG002
            return 0.50

    class Strategy(BaseStrategy):
        def on_start(self):
            self.subscribe(platform="polymarket")

        def on_market_data(self, market, price, orderbook):  # noqa: ARG002
            return

    monkeypatch.setattr("agenttrader.data.parquet_adapter.ParquetDataAdapter", FakeParquet)
    engine = BacktestEngine(data_source=None)
    index = FakeIndex()
    result = engine._run_streaming(
        Strategy,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_cash=1000.0,
            execution_mode=ExecutionMode.SYNTHETIC_EXECUTION_MODEL,
        ),
        index,
    )

    assert result["ok"] is True
    assert result["markets_tested"] == 2
    assert calls["bulk"] == 1


def test_streaming_backtest_broad_polymarket_subscription_uses_all_candidate_ids_without_cap(monkeypatch):
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    polymarket_ids = [f"poly-{idx}" for idx in range(120)]
    kalshi_id = "kalshi-1"
    markets = {
        market_id: Market(
            id=market_id,
            condition_id=market_id,
            platform=Platform.POLYMARKET,
            title=f"Poly {market_id}",
            category="politics",
            tags=[],
            market_type=MarketType.BINARY,
            volume=100.0,
            close_time=now_ts + 86400,
            resolved=False,
            resolution=None,
            scalar_low=None,
            scalar_high=None,
        )
        for market_id in polymarket_ids
    }
    markets[kalshi_id] = Market(
        id=kalshi_id,
        condition_id=kalshi_id,
        platform=Platform.KALSHI,
        title="Kalshi 1",
        category="politics",
        tags=[],
        market_type=MarketType.BINARY,
        volume=80.0,
        close_time=now_ts + 86400,
        resolved=False,
        resolution=None,
        scalar_low=None,
        scalar_high=None,
    )
    bulk_calls: list[tuple[str, list[str]]] = []

    class FakeParquet:
        def __init__(self, *args, **kwargs):
            return

        def is_available(self):
            return True

        def get_markets(self, *args, **kwargs):  # noqa: ARG002
            raise AssertionError("Streaming broad subscription should not use capped get_markets() discovery")

        def get_markets_by_ids(self, market_ids, platform="all"):  # noqa: ARG002
            raise AssertionError("Bulk metadata loader should be preferred for this path")

        def get_markets_by_ids_bulk(self, market_ids, platform="all"):  # noqa: ARG002
            bulk_calls.append((platform, list(market_ids)))
            return [markets[mid] for mid in market_ids if mid in markets]

    class FakeIndex:
        def get_market_rows(self, platform="all", start_ts=None, end_ts=None):  # noqa: ARG002
            rows = [(market_id, "polymarket", 2, now_ts + 60, now_ts + 120) for market_id in polymarket_ids]
            rows.append((kalshi_id, "kalshi", 2, now_ts + 60, now_ts + 120))
            return rows

        def stream_market_history(self, market_id, platform, start_ts, end_ts):  # noqa: ARG002
            yield PricePoint(timestamp=now_ts + 60, yes_price=0.55, no_price=0.45, volume=10.0)

        def stream_market_history_resampled(self, market_id, platform, start_ts, end_ts, bar_seconds):  # noqa: ARG002
            yield from self.stream_market_history(market_id, platform, start_ts, end_ts)

        def get_latest_price_before(self, market_id, platform, ts):  # noqa: ARG002
            return 0.50

    class Strategy(BaseStrategy):
        def on_start(self):
            self.subscribe(platform="polymarket")

        def on_market_data(self, market, price, orderbook):  # noqa: ARG002
            return

    monkeypatch.setattr("agenttrader.data.parquet_adapter.ParquetDataAdapter", FakeParquet)
    engine = BacktestEngine(data_source=None)
    result = engine._run_streaming(
        Strategy,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_cash=1000.0,
            execution_mode=ExecutionMode.SYNTHETIC_EXECUTION_MODEL,
        ),
        FakeIndex(),
    )

    assert result["ok"] is True
    assert result["markets_tested"] == len(polymarket_ids)
    assert len(bulk_calls) == 1
    requested_platform, requested_ids = bulk_calls[0]
    assert requested_platform == "polymarket"
    assert len(requested_ids) == len(polymarket_ids)
    assert set(requested_ids) == set(polymarket_ids)


def test_streaming_backtest_broad_all_subscription_uses_full_candidate_set(monkeypatch):
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    markets = {
        "poly-1": Market(
            id="poly-1",
            condition_id="poly-1",
            platform=Platform.POLYMARKET,
            title="Poly 1",
            category="politics",
            tags=[],
            market_type=MarketType.BINARY,
            volume=100.0,
            close_time=now_ts + 86400,
            resolved=False,
            resolution=None,
            scalar_low=None,
            scalar_high=None,
        ),
        "poly-2": Market(
            id="poly-2",
            condition_id="poly-2",
            platform=Platform.POLYMARKET,
            title="Poly 2",
            category="sports",
            tags=[],
            market_type=MarketType.BINARY,
            volume=90.0,
            close_time=now_ts + 86400,
            resolved=False,
            resolution=None,
            scalar_low=None,
            scalar_high=None,
        ),
        "kalshi-1": Market(
            id="kalshi-1",
            condition_id="kalshi-1",
            platform=Platform.KALSHI,
            title="Kalshi 1",
            category="politics",
            tags=[],
            market_type=MarketType.BINARY,
            volume=70.0,
            close_time=now_ts + 86400,
            resolved=False,
            resolution=None,
            scalar_low=None,
            scalar_high=None,
        ),
    }

    class FakeParquet:
        def __init__(self, *args, **kwargs):
            return

        def is_available(self):
            return True

        def get_markets(self, *args, **kwargs):  # noqa: ARG002
            raise AssertionError("Streaming broad subscription should not use capped get_markets() discovery")

        def get_markets_by_ids(self, market_ids, platform="all"):  # noqa: ARG002
            return [markets[mid] for mid in market_ids if mid in markets]

    class FakeIndex:
        def get_market_ids_with_counts(self, platform="all", start_ts=None, end_ts=None):  # noqa: ARG002
            return [("poly-1", "polymarket", 5), ("poly-2", "polymarket", 4), ("kalshi-1", "kalshi", 3)]

        def stream_market_history(self, market_id, platform, start_ts, end_ts):  # noqa: ARG002
            yield PricePoint(timestamp=now_ts + 60, yes_price=0.52, no_price=0.48, volume=5.0)

        def stream_market_history_resampled(self, market_id, platform, start_ts, end_ts, bar_seconds):  # noqa: ARG002
            yield from self.stream_market_history(market_id, platform, start_ts, end_ts)

        def get_latest_price_before(self, market_id, platform, ts):  # noqa: ARG002
            return 0.50

    class Strategy(BaseStrategy):
        def on_start(self):
            self.subscribe(platform="all")

        def on_market_data(self, market, price, orderbook):  # noqa: ARG002
            return

    monkeypatch.setattr("agenttrader.data.parquet_adapter.ParquetDataAdapter", FakeParquet)
    engine = BacktestEngine(data_source=None)
    result = engine._run_streaming(
        Strategy,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_cash=1000.0,
            execution_mode=ExecutionMode.SYNTHETIC_EXECUTION_MODEL,
        ),
        FakeIndex(),
    )

    assert result["ok"] is True
    assert result["markets_tested"] == 3


def test_streaming_backtest_broad_category_and_tags_subscription_uses_candidate_metadata(monkeypatch):
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    markets = {
        "poly-1": Market(
            id="poly-1",
            condition_id="poly-1",
            platform=Platform.POLYMARKET,
            title="Poly 1",
            category="politics",
            tags=["featured"],
            market_type=MarketType.BINARY,
            volume=100.0,
            close_time=now_ts + 86400,
            resolved=False,
            resolution=None,
            scalar_low=None,
            scalar_high=None,
        ),
        "poly-2": Market(
            id="poly-2",
            condition_id="poly-2",
            platform=Platform.POLYMARKET,
            title="Poly 2",
            category="politics",
            tags=[],
            market_type=MarketType.BINARY,
            volume=90.0,
            close_time=now_ts + 86400,
            resolved=False,
            resolution=None,
            scalar_low=None,
            scalar_high=None,
        ),
        "poly-3": Market(
            id="poly-3",
            condition_id="poly-3",
            platform=Platform.POLYMARKET,
            title="Poly 3",
            category="sports",
            tags=["featured"],
            market_type=MarketType.BINARY,
            volume=80.0,
            close_time=now_ts + 86400,
            resolved=False,
            resolution=None,
            scalar_low=None,
            scalar_high=None,
        ),
    }
    requested_chunks: list[list[str]] = []

    class FakeParquet:
        def __init__(self, *args, **kwargs):
            return

        def is_available(self):
            return True

        def get_markets(self, *args, **kwargs):  # noqa: ARG002
            raise AssertionError("Streaming broad subscription should not use capped get_markets() discovery")

        def get_markets_by_ids(self, market_ids, platform="all"):  # noqa: ARG002
            requested_chunks.append(list(market_ids))
            return [markets[mid] for mid in market_ids if mid in markets]

    class FakeIndex:
        def get_market_ids_with_counts(self, platform="all", start_ts=None, end_ts=None):  # noqa: ARG002
            return [("poly-1", "polymarket", 5), ("poly-2", "polymarket", 4), ("poly-3", "polymarket", 3)]

        def stream_market_history(self, market_id, platform, start_ts, end_ts):  # noqa: ARG002
            yield PricePoint(timestamp=now_ts + 60, yes_price=0.52, no_price=0.48, volume=5.0)

        def stream_market_history_resampled(self, market_id, platform, start_ts, end_ts, bar_seconds):  # noqa: ARG002
            yield from self.stream_market_history(market_id, platform, start_ts, end_ts)

        def get_latest_price_before(self, market_id, platform, ts):  # noqa: ARG002
            return 0.50

    class Strategy(BaseStrategy):
        def on_start(self):
            self.subscribe(platform="polymarket", category="politics", tags=["featured"])

        def on_market_data(self, market, price, orderbook):  # noqa: ARG002
            return

    monkeypatch.setattr("agenttrader.data.parquet_adapter.ParquetDataAdapter", FakeParquet)
    engine = BacktestEngine(data_source=None)
    result = engine._run_streaming(
        Strategy,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_cash=1000.0,
            execution_mode=ExecutionMode.SYNTHETIC_EXECUTION_MODEL,
        ),
        FakeIndex(),
    )

    assert result["ok"] is True
    assert result["markets_tested"] == 1
    assert requested_chunks


def test_streaming_backtest_skips_warmup_queries_when_start_is_at_earliest_candidate_timestamp(monkeypatch):
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    markets = {
        "poly-1": Market(
            id="poly-1",
            condition_id="poly-1",
            platform=Platform.POLYMARKET,
            title="Poly 1",
            category="politics",
            tags=[],
            market_type=MarketType.BINARY,
            volume=100.0,
            close_time=now_ts + 86400,
            resolved=False,
            resolution=None,
            scalar_low=None,
            scalar_high=None,
        ),
        "poly-2": Market(
            id="poly-2",
            condition_id="poly-2",
            platform=Platform.POLYMARKET,
            title="Poly 2",
            category="politics",
            tags=[],
            market_type=MarketType.BINARY,
            volume=90.0,
            close_time=now_ts + 86400,
            resolved=False,
            resolution=None,
            scalar_low=None,
            scalar_high=None,
        ),
    }

    class FakeParquet:
        def __init__(self, *args, **kwargs):
            return

        def is_available(self):
            return True

        def get_markets(self, *args, **kwargs):  # noqa: ARG002
            raise AssertionError("Streaming broad subscription should not use capped get_markets() discovery")

        def get_markets_by_ids(self, market_ids, platform="all"):  # noqa: ARG002
            return [markets[mid] for mid in market_ids if mid in markets]

    class FakeIndex:
        def __init__(self):
            self.warmup_calls = 0

        def get_market_rows(self, platform="all", start_ts=None, end_ts=None):  # noqa: ARG002
            return [
                ("poly-1", "polymarket", 5, now_ts + 60, now_ts + 180),
                ("poly-2", "polymarket", 4, now_ts + 120, now_ts + 180),
            ]

        def get_latest_prices_before_batch(self, market_ids, platform, ts):  # noqa: ARG002
            self.warmup_calls += 1
            raise AssertionError("Warmup should be skipped when no prior data can exist")

        def stream_market_history_batch(self, market_ids, platform, start_ts, end_ts):  # noqa: ARG002
            for market_id in market_ids:
                yield (market_id, PricePoint(timestamp=now_ts + 60, yes_price=0.55, no_price=0.45, volume=10.0))

        def stream_market_history_resampled_batch(self, market_ids, platform, start_ts, end_ts, bar_seconds):  # noqa: ARG002
            yield from self.stream_market_history_batch(market_ids, platform, start_ts, end_ts)

    class Strategy(BaseStrategy):
        def on_start(self):
            self.subscribe(platform="polymarket")

        def on_market_data(self, market, price, orderbook):  # noqa: ARG002
            return

    monkeypatch.setattr("agenttrader.data.parquet_adapter.ParquetDataAdapter", FakeParquet)
    engine = BacktestEngine(data_source=None)
    index = FakeIndex()
    result = engine._run_streaming(
        Strategy,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_cash=1000.0,
            execution_mode=ExecutionMode.SYNTHETIC_EXECUTION_MODEL,
        ),
        index,
    )

    assert result["ok"] is True
    assert result["markets_tested"] == 2
    assert index.warmup_calls == 0


def test_legacy_backtest_hydrates_missing_exact_id_metadata(monkeypatch):
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    low_volume_market = Market(
        id="KXELONMARS-99",
        condition_id="KXELONMARS-99",
        platform=Platform.KALSHI,
        title="Will Elon visit Mars?",
        category="world",
        tags=["World", "International", "kxelonmars"],
        market_type=MarketType.BINARY,
        volume=5.0,
        close_time=now_ts + 86400,
        resolved=False,
        resolution=None,
        scalar_low=None,
        scalar_high=None,
    )
    high_volume_market = Market(
        id="KXTRUMPOUT-26-TRUMP",
        condition_id="KXTRUMPOUT-26-TRUMP",
        platform=Platform.KALSHI,
        title="Will Trump drop out?",
        category="politics",
        tags=["Politics"],
        market_type=MarketType.BINARY,
        volume=10_000.0,
        close_time=now_ts + 86400,
        resolved=False,
        resolution=None,
        scalar_low=None,
        scalar_high=None,
    )

    class FakeData:
        def __init__(self):
            self.get_markets_by_ids_calls = 0

        def get_markets(self, platform="all", limit=10000):  # noqa: ARG002
            return [high_volume_market]

        def get_markets_by_ids(self, market_ids, platform="all"):  # noqa: ARG002
            self.get_markets_by_ids_calls += 1
            if "KXELONMARS-99" in set(market_ids):
                return [low_volume_market]
            return []

        def get_price_history(self, market_id, start_ts, end_ts):  # noqa: ARG002
            if market_id != "KXELONMARS-99":
                return []
            return [PricePoint(timestamp=now_ts + 60, yes_price=0.55, no_price=0.45, volume=10.0)]

    class Strategy(BaseStrategy):
        def on_start(self):
            self.subscribe(platform="kalshi", market_ids=["KXELONMARS-99"])

        def on_market_data(self, market, price, orderbook):  # noqa: ARG002
            return

    monkeypatch.setattr("agenttrader.core.context.BacktestContext.get_orderbook", lambda self, market_id: None)

    data = FakeData()
    engine = BacktestEngine(data_source=data, orderbook_store=None)
    result = engine._run_legacy(
        Strategy,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-01",
            initial_cash=1000.0,
            schedule_interval_minutes=1440,
        ),
    )

    assert result["ok"] is True
    assert result["markets_tested"] == 1
    assert data.get_markets_by_ids_calls == 1


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

        def get_markets_by_ids(self, market_ids, platform="all"):  # noqa: ARG002
            return [market for market in markets if market.id in set(market_ids)]

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
            execution_mode=ExecutionMode.SYNTHETIC_EXECUTION_MODEL,
        ),
        index,
    )
    assert result["ok"] is True
    assert result["fidelity"] == "bar_1h"
    assert result["max_markets_applied"] == 1
    assert result["markets_tested"] == 1
    assert index.resampled_calls >= 1
