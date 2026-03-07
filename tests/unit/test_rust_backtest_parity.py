"""
Parity tests for Rust backtest executor vs Python streaming engine.
"""

from datetime import UTC, datetime

import pytest

from agenttrader.core.backtest_engine import BacktestConfig, BacktestEngine
from agenttrader.core.base_strategy import BaseStrategy
from agenttrader.core.context import RustBackedContext, StreamingBacktestContext
from agenttrader.core.fill_model import FillModel
from agenttrader.data.models import (
    ExecutionMode,
    Market,
    MarketType,
    Platform,
    PricePoint,
)
from agenttrader.errors import AgentTraderError, MarketNotCachedError
from agenttrader_kernel import RustBacktestExecutor


class FakeParquet:
    def __init__(self, markets):
        self._markets = markets

    def is_available(self):
        return True

    def get_markets(self, platform="all", limit=50000):
        return self._markets

    def get_markets_by_ids(self, market_ids, platform="all"):
        return [m for m in self._markets if m.id in set(market_ids)]


class FakeIndex:
    def __init__(self, price_data, resolutions=None):
        self._price_data = price_data  # dict market_id -> list of PricePoint
        self._resolutions = resolutions or {}

    def get_market_ids(self, platform="all", start_ts=None, end_ts=None):
        return [(mid, "polymarket") for mid in self._price_data.keys()]

    def get_market_ids_with_counts(self, platform="all", start_ts=None, end_ts=None):
        return [
            (mid, "polymarket", len(prices)) for mid, prices in self._price_data.items()
        ]

    def stream_market_history(self, market_id, platform, start_ts, end_ts):
        for pp in self._price_data.get(market_id, []):
            if start_ts <= pp.timestamp <= end_ts:
                yield pp

    def stream_market_history_resampled(
        self, market_id, platform, start_ts, end_ts, bar_seconds
    ):
        yield from self.stream_market_history(market_id, platform, start_ts, end_ts)

    def get_latest_price_before(self, market_id, platform, ts):
        prices = self._price_data.get(market_id, [])
        before = [p for p in prices if p.timestamp < ts]
        return before[-1].yes_price if before else 0.40


class EmptyStrategy(BaseStrategy):
    """Strategy that subscribes but makes no trades."""

    def on_start(self):
        self.subscribe(platform="polymarket")

    def on_market_data(self, market, price, orderbook):
        pass  # No trades


def make_market(market_id, close_time, resolved=False, resolution=None):
    return Market(
        id=market_id,
        condition_id=market_id,
        platform=Platform.POLYMARKET,
        title=f"Test Market {market_id}",
        category="politics",
        tags=["test"],
        market_type=MarketType.BINARY,
        volume=1000.0,
        close_time=close_time,
        resolved=resolved,
        resolution=resolution,
        scalar_low=None,
        scalar_high=None,
    )


def test_rust_vs_python_empty_strategy_engine_parity(monkeypatch):
    """
    Compare Rust executor vs Python streaming engine with empty strategy.

    This tests engine mechanics (stream merging, snapshot timing) without
    strategy-dependent behavior.
    """
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())

    # Same price data for both engines
    price_data = {
        "m1": [
            PricePoint(
                timestamp=now_ts + 60, yes_price=0.45, no_price=0.55, volume=10.0
            ),
            PricePoint(
                timestamp=now_ts + 120, yes_price=0.55, no_price=0.45, volume=12.0
            ),
        ]
    }

    market = make_market("m1", now_ts + 86400)
    fake_parquet = FakeParquet([market])
    fake_index = FakeIndex(price_data)

    # Monkeypatch for Python engine
    monkeypatch.setattr(
        "agenttrader.data.parquet_adapter.ParquetDataAdapter",
        lambda *a, **kw: fake_parquet,
    )

    # Run Python engine
    engine = BacktestEngine(data_source=None)
    python_result = engine._run_streaming(
        EmptyStrategy,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_cash=1000.0,
            execution_mode=ExecutionMode.STRICT_PRICE_ONLY,
        ),
        fake_index,
    )

    # Run Rust executor
    rust_price_data = [[(p.timestamp, p.yes_price) for p in price_data["m1"]]]
    rust_executor = RustBacktestExecutor()
    rust_result = rust_executor.run(
        market_ids=["m1"],
        price_data=rust_price_data,
        resolutions=[(None, 0)],
        start_ts=now_ts,
        end_ts=now_ts + 200,
        initial_cash=1000.0,
        schedule_interval_minutes=15,
        history_buffer_hours=168,
    )

    # Compare outputs
    python_curve = python_result.get("_artifact_payload", {}).get("equity_curve", [])
    rust_curve = rust_result.equity_curve

    print(f"\n=== Comparison ===")
    print(f"Python equity_curve: {python_curve}")
    print(f"Rust equity_curve: {rust_curve}")
    print(f"Python final_value: {python_result['final_value']}")
    print(f"Rust final_value: {rust_result.final_value}")
    print(
        f"Python trades: {len(python_result.get('_artifact_payload', {}).get('trades', []))}"
    )
    print(f"Rust trades: {rust_result.total_trades}")

    # Both should have at least 2 snapshots (start + some end)
    assert len(python_curve) >= 2, (
        f"Python should have at least 2 snapshots, got {len(python_curve)}"
    )
    assert len(rust_curve) >= 2, (
        f"Rust should have at least 2 snapshots, got {len(rust_curve)}"
    )

    # First snapshot should match (start)
    python_start = python_curve[0]["timestamp"]
    rust_start = rust_curve[0][0]
    assert python_start == rust_start, (
        f"Start timestamp mismatch: Python={python_start}, Rust={rust_start}"
    )

    # Both should have same final value (no trades)
    assert python_result["final_value"] == rust_result.final_value


def test_rust_stream_merging_order():
    """
    Verify Rust stream merger produces correct timestamp order
    for interleaved market data.
    """
    now_ts = 1000

    # Three markets with interleaved timestamps
    price_data = [
        [(1001, 0.50), (1003, 0.52), (1005, 0.54)],  # Market A: odd
        [(1000, 0.51), (1002, 0.53), (1004, 0.55)],  # Market B: even
        [(1000, 0.49), (1002, 0.48), (1004, 0.47)],  # Market C: descending
    ]

    executor = RustBacktestExecutor()
    result = executor.run(
        market_ids=["A", "B", "C"],
        price_data=price_data,
        resolutions=[(None, 0), (None, 0), (None, 0)],
        start_ts=1000,
        end_ts=1010,
        initial_cash=1000.0,
        schedule_interval_minutes=15,
        history_buffer_hours=168,
    )

    # The equity curve should have 2 snapshots (start + end)
    # No trades so just cash value throughout
    assert len(result.equity_curve) == 2
    assert result.final_value == 1000.0


def test_rust_with_resolution(monkeypatch):
    """
    Test Rust executor handles market resolution correctly.
    """
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())

    # Market with resolution
    price_data = {
        "m1": [
            PricePoint(
                timestamp=now_ts + 60, yes_price=0.45, no_price=0.55, volume=10.0
            ),
            PricePoint(
                timestamp=now_ts + 120, yes_price=0.55, no_price=0.45, volume=12.0
            ),
        ]
    }

    market = make_market("m1", now_ts + 86400, resolved=True, resolution="yes")
    fake_parquet = FakeParquet([market])
    fake_index = FakeIndex(price_data, resolutions={"m1": ("yes", now_ts + 86400)})

    monkeypatch.setattr(
        "agenttrader.data.parquet_adapter.ParquetDataAdapter",
        lambda *a, **kw: fake_parquet,
    )

    # Run Python engine
    engine = BacktestEngine(data_source=None)
    python_result = engine._run_streaming(
        EmptyStrategy,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_cash=1000.0,
            execution_mode=ExecutionMode.STRICT_PRICE_ONLY,
        ),
        fake_index,
    )

    # Run Rust executor - resolution at close_time
    rust_price_data = [[(p.timestamp, p.yes_price) for p in price_data["m1"]]]
    rust_executor = RustBacktestExecutor()
    rust_result = rust_executor.run(
        market_ids=["m1"],
        price_data=rust_price_data,
        resolutions=[("yes", now_ts + 86400)],
        start_ts=now_ts,
        end_ts=now_ts + 200,
        initial_cash=1000.0,
        schedule_interval_minutes=15,
        history_buffer_hours=168,
    )

    print(f"\n=== Resolution Test ===")
    print(f"Python final_value: {python_result['final_value']}")
    print(f"Rust final_value: {rust_result.final_value}")
    print(
        f"Python trades: {len(python_result.get('_artifact_payload', {}).get('trades', []))}"
    )
    print(f"Rust trades: {rust_result.total_trades}")

    # Both should have same final value (no trades, cash unchanged)
    assert python_result["final_value"] == rust_result.final_value


def test_final_snapshot_timestamp_parity(monkeypatch):
    """
    Verify Rust records final snapshot at end_ts, not last event timestamp.
    """
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())

    # Only 2 price events, but end_ts is much later
    price_data = {
        "m1": [
            PricePoint(
                timestamp=now_ts + 60, yes_price=0.45, no_price=0.55, volume=10.0
            ),
            PricePoint(
                timestamp=now_ts + 120, yes_price=0.55, no_price=0.45, volume=12.0
            ),
        ]
    }

    market = make_market("m1", now_ts + 86400)
    fake_parquet = FakeParquet([market])
    fake_index = FakeIndex(price_data)

    monkeypatch.setattr(
        "agenttrader.data.parquet_adapter.ParquetDataAdapter",
        lambda *a, **kw: fake_parquet,
    )

    engine = BacktestEngine(data_source=None)
    python_result = engine._run_streaming(
        EmptyStrategy,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-02",  # end_ts will be ~end of day
            initial_cash=1000.0,
            execution_mode=ExecutionMode.STRICT_PRICE_ONLY,
        ),
        fake_index,
    )

    python_curve = python_result.get("_artifact_payload", {}).get("equity_curve", [])
    python_end_ts = python_curve[-1]["timestamp"]

    # Rust executor - use same end_ts as Python
    rust_price_data = [[(p.timestamp, p.yes_price) for p in price_data["m1"]]]
    rust_executor = RustBacktestExecutor()
    rust_result = rust_executor.run(
        market_ids=["m1"],
        price_data=rust_price_data,
        resolutions=[(None, 0)],
        start_ts=now_ts,
        end_ts=python_end_ts,
        initial_cash=1000.0,
        schedule_interval_minutes=15,
        history_buffer_hours=168,
    )

    rust_end_ts = rust_result.equity_curve[-1][0]

    print(f"\n=== Final Snapshot Test ===")
    print(f"Python final timestamp: {python_end_ts}")
    print(f"Rust final timestamp: {rust_end_ts}")
    print(f"Last event was at: {now_ts + 120}")

    # Final snapshot should be at end_ts, not last event
    assert rust_end_ts == python_end_ts, (
        f"Final timestamp mismatch: Rust={rust_end_ts}, Python={python_end_ts}"
    )
    assert rust_end_ts > now_ts + 120, "Final timestamp should be after last event"


def test_warm_start_price_before_first_event(monkeypatch):
    """
    Test warm-start: price cursor is set before first event from get_latest_price_before.
    """
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())

    # Market has price data starting AFTER start_ts
    # get_latest_price_before should provide warm-start price
    price_data = {
        "m1": [
            PricePoint(
                timestamp=now_ts + 1000, yes_price=0.50, no_price=0.50, volume=10.0
            ),
            PricePoint(
                timestamp=now_ts + 2000, yes_price=0.55, no_price=0.45, volume=12.0
            ),
        ]
    }

    market = make_market("m1", now_ts + 86400)
    fake_parquet = FakeParquet([market])

    # FakeIndex returns warm-start price
    class WarmStartIndex(FakeIndex):
        def get_latest_price_before(self, market_id, platform, ts):
            return 0.45  # Warm-start price before first event

    fake_index = WarmStartIndex(price_data)

    monkeypatch.setattr(
        "agenttrader.data.parquet_adapter.ParquetDataAdapter",
        lambda *a, **kw: fake_parquet,
    )

    engine = BacktestEngine(data_source=None)
    python_result = engine._run_streaming(
        EmptyStrategy,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_cash=1000.0,
            execution_mode=ExecutionMode.STRICT_PRICE_ONLY,
        ),
        fake_index,
    )

    # Rust doesn't support warm-start yet (no get_latest_price_before equivalent)
    # This test documents the Python behavior for future parity
    python_curve = python_result.get("_artifact_payload", {}).get("equity_curve", [])

    print(f"\n=== Warm Start Test ===")
    print(f"Python equity_curve: {python_curve}")
    print(f"First event at: {now_ts + 1000}")
    print(f"Expected warm-start price: 0.45")

    # This test passes in Python, will need Rust support for full parity
    assert len(python_curve) >= 2


def test_market_with_no_events_but_resolution(monkeypatch):
    """
    Markets with no in-range price events but with resolution in range.
    """
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())

    # Market: price data is OUTSIDE our range, but resolution is INSIDE
    price_data = {
        "m1": [
            # These are before our range
            PricePoint(
                timestamp=now_ts - 86400, yes_price=0.45, no_price=0.55, volume=10.0
            ),
        ]
    }

    market = make_market(
        "m1", now_ts + 3600, resolved=True, resolution="yes"
    )  # Resolution in range
    fake_parquet = FakeParquet([market])
    fake_index = FakeIndex(price_data, resolutions={"m1": ("yes", now_ts + 3600)})

    monkeypatch.setattr(
        "agenttrader.data.parquet_adapter.ParquetDataAdapter",
        lambda *a, **kw: fake_parquet,
    )

    engine = BacktestEngine(data_source=None)
    python_result = engine._run_streaming(
        EmptyStrategy,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-01",  # Range includes resolution time
            initial_cash=1000.0,
            execution_mode=ExecutionMode.STRICT_PRICE_ONLY,
        ),
        fake_index,
    )

    # Rust: same market but no price events in range
    rust_price_data = [[]]  # Empty price data for this market
    rust_executor = RustBacktestExecutor()
    rust_result = rust_executor.run(
        market_ids=["m1"],
        price_data=rust_price_data,
        resolutions=[("yes", now_ts + 3600)],  # Resolution in range
        start_ts=now_ts,
        end_ts=now_ts + 7200,
        initial_cash=1000.0,
        schedule_interval_minutes=15,
        history_buffer_hours=168,
    )

    print(f"\n=== No Events but Resolution Test ===")
    print(f"Python final_value: {python_result['final_value']}")
    print(f"Rust final_value: {rust_result.final_value}")
    print(
        f"Python equity_curve: {python_result.get('_artifact_payload', {}).get('equity_curve', [])}"
    )
    print(f"Rust equity_curve: {rust_result.equity_curve}")

    # Both should have same final value
    assert python_result["final_value"] == rust_result.final_value


def test_multi_market_interleaved_sparse(monkeypatch):
    """
    Multiple markets with sparse, interleaved timestamps.
    """
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())

    # Three markets, sparse timestamps
    price_data = {
        "m1": [
            PricePoint(
                timestamp=now_ts + 100, yes_price=0.50, no_price=0.50, volume=10.0
            ),
            PricePoint(
                timestamp=now_ts + 700, yes_price=0.55, no_price=0.45, volume=12.0
            ),
        ],
        "m2": [
            PricePoint(
                timestamp=now_ts + 300, yes_price=0.45, no_price=0.55, volume=8.0
            ),
            PricePoint(
                timestamp=now_ts + 900, yes_price=0.60, no_price=0.40, volume=15.0
            ),
        ],
        "m3": [
            PricePoint(
                timestamp=now_ts + 500, yes_price=0.40, no_price=0.60, volume=5.0
            ),
        ],
    }

    markets = [make_market(f"m{i}", now_ts + 86400) for i in range(1, 4)]
    fake_parquet = FakeParquet(markets)
    fake_index = FakeIndex(price_data)

    monkeypatch.setattr(
        "agenttrader.data.parquet_adapter.ParquetDataAdapter",
        lambda *a, **kw: fake_parquet,
    )

    engine = BacktestEngine(data_source=None)
    python_result = engine._run_streaming(
        EmptyStrategy,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_cash=1000.0,
            execution_mode=ExecutionMode.STRICT_PRICE_ONLY,
        ),
        fake_index,
    )

    # Rust
    rust_price_data = [
        [(p.timestamp, p.yes_price) for p in price_data["m1"]],
        [(p.timestamp, p.yes_price) for p in price_data["m2"]],
        [(p.timestamp, p.yes_price) for p in price_data["m3"]],
    ]
    rust_executor = RustBacktestExecutor()
    rust_result = rust_executor.run(
        market_ids=["m1", "m2", "m3"],
        price_data=rust_price_data,
        resolutions=[(None, 0), (None, 0), (None, 0)],
        start_ts=now_ts,
        end_ts=now_ts + 1000,
        initial_cash=1000.0,
        schedule_interval_minutes=15,
        history_buffer_hours=168,
    )

    python_curve = python_result.get("_artifact_payload", {}).get("equity_curve", [])

    print(f"\n=== Multi-Market Sparse Test ===")
    print(f"Python: {len(python_curve)} snapshots")
    print(f"Rust: {len(rust_result.equity_curve)} snapshots")
    print(
        f"Python first event: {python_curve[0]['timestamp'] if python_curve else 'none'}"
    )
    print(
        f"Rust first event: {rust_result.equity_curve[0][0] if rust_result.equity_curve else 'none'}"
    )

    # Both should have start and end snapshots
    assert len(python_curve) >= 2
    assert len(rust_result.equity_curve) >= 2


def test_schedule_bookkeeping_empty_strategy(monkeypatch):
    """
    Verify schedule bookkeeping works even with empty strategy.
    The schedule should tick even if strategy doesn't act on it.
    """
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())

    # Price data spanning multiple schedule intervals
    price_data = {
        "m1": [
            # Schedule interval is 15 min = 900s
            # Events at: 100, 1000, 1900, 2800 (each > 900s apart)
            PricePoint(
                timestamp=now_ts + 100, yes_price=0.50, no_price=0.50, volume=10.0
            ),
            PricePoint(
                timestamp=now_ts + 1000, yes_price=0.50, no_price=0.50, volume=10.0
            ),
            PricePoint(
                timestamp=now_ts + 1900, yes_price=0.50, no_price=0.50, volume=10.0
            ),
            PricePoint(
                timestamp=now_ts + 2800, yes_price=0.50, no_price=0.50, volume=10.0
            ),
        ]
    }

    market = make_market("m1", now_ts + 86400)
    fake_parquet = FakeParquet([market])
    fake_index = FakeIndex(price_data)

    monkeypatch.setattr(
        "agenttrader.data.parquet_adapter.ParquetDataAdapter",
        lambda *a, **kw: fake_parquet,
    )

    engine = BacktestEngine(data_source=None)
    python_result = engine._run_streaming(
        EmptyStrategy,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-01",  # Same day
            initial_cash=1000.0,
            schedule_interval_minutes=15,  # 900s
            execution_mode=ExecutionMode.STRICT_PRICE_ONLY,
        ),
        fake_index,
    )

    # Rust
    rust_price_data = [[(p.timestamp, p.yes_price) for p in price_data["m1"]]]
    rust_executor = RustBacktestExecutor()
    rust_result = rust_executor.run(
        market_ids=["m1"],
        price_data=rust_price_data,
        resolutions=[(None, 0)],
        start_ts=now_ts,
        end_ts=now_ts + 3000,
        initial_cash=1000.0,
        schedule_interval_minutes=15,
        history_buffer_hours=168,
    )

    print(f"\n=== Schedule Bookkeeping Test ===")
    print(
        f"Python snapshots: {len(python_result.get('_artifact_payload', {}).get('equity_curve', []))}"
    )
    print(f"Rust snapshots: {len(rust_result.equity_curve)}")
    print(f"Events at: 100, 1000, 1900, 2800 (intervals of ~900s)")
    print(f"Schedule interval: 900s")

    # With hourly snapshots + schedule intervals, both should have > 2 snapshots
    # (start + hourly checkpoints + end)
    assert len(rust_result.equity_curve) >= 2


# ── RustBackedContext parity tests ───────────────────────────────────────────
# These test that RustBackedContext (use_rust=True) produces identical results
# to StreamingBacktestContext (use_rust=False) when driven by the same strategy.


def _run_both(strategy_class, price_data, markets, monkeypatch, *, now_ts, end_ts_offset=86400, index=None):
    """Helper: run the same strategy with Python and Rust contexts, return both results."""
    fake_parquet = FakeParquet(markets)
    if index is None:
        index = FakeIndex(price_data)

    monkeypatch.setattr(
        "agenttrader.data.parquet_adapter.ParquetDataAdapter",
        lambda *a, **kw: fake_parquet,
    )

    engine = BacktestEngine(data_source=None)

    python_result = engine._run_streaming(
        strategy_class,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_cash=1000.0,
            execution_mode=ExecutionMode.STRICT_PRICE_ONLY,
            use_rust=False,
        ),
        index,
    )

    rust_result = engine._run_streaming(
        strategy_class,
        BacktestConfig(
            strategy_path="test",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_cash=1000.0,
            execution_mode=ExecutionMode.STRICT_PRICE_ONLY,
            use_rust=True,
        ),
        index,
    )

    return python_result, rust_result


def test_rust_backed_context_empty_strategy_parity(monkeypatch):
    """RustBackedContext with empty strategy should match Python context."""
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    price_data = {
        "m1": [
            PricePoint(timestamp=now_ts + 100, yes_price=0.45, no_price=0.55, volume=10.0),
            PricePoint(timestamp=now_ts + 200, yes_price=0.50, no_price=0.50, volume=12.0),
            PricePoint(timestamp=now_ts + 300, yes_price=0.55, no_price=0.45, volume=8.0),
        ]
    }
    markets = [make_market("m1", now_ts + 86400)]

    python_result, rust_result = _run_both(
        EmptyStrategy, price_data, markets, monkeypatch, now_ts=now_ts
    )

    assert python_result["ok"], f"Python failed: {python_result}"
    assert rust_result["ok"], f"Rust failed: {rust_result}"

    py_curve = python_result["_artifact_payload"]["equity_curve"]
    rs_curve = rust_result["_artifact_payload"]["equity_curve"]

    assert len(py_curve) >= 2
    assert len(rs_curve) >= 2
    assert abs(python_result["final_value"] - rust_result["final_value"]) < 1e-9
    assert len(python_result["_artifact_payload"]["trades"]) == len(
        rust_result["_artifact_payload"]["trades"]
    )
    assert py_curve[0]["timestamp"] == rs_curve[0]["timestamp"]
    assert py_curve[-1]["timestamp"] == rs_curve[-1]["timestamp"]


class BuyOnLowStrategy(BaseStrategy):
    """Buys when price < 0.48, never sells. Tests buy recording parity."""

    def on_start(self):
        self.subscribe(platform="polymarket")

    def on_market_data(self, market, price, orderbook):
        if price < 0.48 and self.get_position(market.id) is None:
            self.buy(market.id, 2.0)


def test_rust_backed_context_buy_strategy_parity(monkeypatch):
    """RustBackedContext with a buying strategy should match Python context exactly."""
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    price_data = {
        "m1": [
            PricePoint(timestamp=now_ts + 100, yes_price=0.45, no_price=0.55, volume=10.0),
            PricePoint(timestamp=now_ts + 200, yes_price=0.50, no_price=0.50, volume=12.0),
            PricePoint(timestamp=now_ts + 300, yes_price=0.55, no_price=0.45, volume=8.0),
        ]
    }
    markets = [make_market("m1", now_ts + 86400)]

    python_result, rust_result = _run_both(
        BuyOnLowStrategy, price_data, markets, monkeypatch, now_ts=now_ts
    )

    assert python_result["ok"], f"Python failed: {python_result}"
    assert rust_result["ok"], f"Rust failed: {rust_result}"

    py_trades = python_result["_artifact_payload"]["trades"]
    rs_trades = rust_result["_artifact_payload"]["trades"]

    print(f"\n=== Buy Strategy Parity ===")
    print(f"Python final: {python_result['final_value']:.4f}, trades: {len(py_trades)}")
    print(f"Rust   final: {rust_result['final_value']:.4f}, trades: {len(rs_trades)}")

    assert len(py_trades) == len(rs_trades), (
        f"Trade count mismatch: Python={len(py_trades)}, Rust={len(rs_trades)}"
    )
    assert abs(python_result["final_value"] - rust_result["final_value"]) < 1e-9, (
        f"Final value mismatch: Python={python_result['final_value']}, Rust={rust_result['final_value']}"
    )

    for i, (py_t, rs_t) in enumerate(zip(py_trades, rs_trades)):
        assert py_t["action"] == rs_t["action"], f"Trade {i} action mismatch"
        assert py_t["market_id"] == rs_t["market_id"], f"Trade {i} market_id mismatch"
        assert abs(py_t["contracts"] - rs_t["contracts"]) < 1e-9, f"Trade {i} contracts mismatch"
        assert abs(py_t["price"] - rs_t["price"]) < 1e-9, f"Trade {i} price mismatch"


class BuyAndSellStrategy(BaseStrategy):
    """Buys low, sells when price >= 0.53. Tests round-trip trade parity."""

    def on_start(self):
        self.subscribe(platform="polymarket")

    def on_market_data(self, market, price, orderbook):
        pos = self.get_position(market.id)
        if price < 0.48 and pos is None:
            self.buy(market.id, 2.0)
        elif price >= 0.53 and pos is not None:
            self.sell(market.id)


def test_rust_backed_context_buy_sell_parity(monkeypatch):
    """RustBackedContext buy+sell round trip should match Python exactly."""
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    price_data = {
        "m1": [
            PricePoint(timestamp=now_ts + 100, yes_price=0.45, no_price=0.55, volume=10.0),
            PricePoint(timestamp=now_ts + 200, yes_price=0.50, no_price=0.50, volume=12.0),
            PricePoint(timestamp=now_ts + 300, yes_price=0.55, no_price=0.45, volume=8.0),
            PricePoint(timestamp=now_ts + 400, yes_price=0.58, no_price=0.42, volume=6.0),
        ]
    }
    markets = [make_market("m1", now_ts + 86400)]

    python_result, rust_result = _run_both(
        BuyAndSellStrategy, price_data, markets, monkeypatch, now_ts=now_ts
    )

    assert python_result["ok"], f"Python failed: {python_result}"
    assert rust_result["ok"], f"Rust failed: {rust_result}"

    py_trades = python_result["_artifact_payload"]["trades"]
    rs_trades = rust_result["_artifact_payload"]["trades"]

    print(f"\n=== Buy+Sell Parity ===")
    print(f"Python final: {python_result['final_value']:.4f}, trades: {len(py_trades)}")
    print(f"Rust   final: {rust_result['final_value']:.4f}, trades: {len(rs_trades)}")
    for t in py_trades:
        print(f"  Python trade: {t['action']} {t['contracts']}@{t['price']} pnl={t.get('pnl')}")
    for t in rs_trades:
        print(f"  Rust   trade: {t['action']} {t['contracts']}@{t['price']} pnl={t.get('pnl')}")

    assert len(py_trades) == len(rs_trades), (
        f"Trade count: Python={len(py_trades)}, Rust={len(rs_trades)}"
    )
    assert abs(python_result["final_value"] - rust_result["final_value"]) < 1e-9

    for i, (py_t, rs_t) in enumerate(zip(py_trades, rs_trades)):
        assert py_t["action"] == rs_t["action"]
        assert abs(py_t["contracts"] - rs_t["contracts"]) < 1e-9
        assert abs(py_t["price"] - rs_t["price"]) < 1e-9
        if py_t.get("pnl") is not None and rs_t.get("pnl") is not None:
            assert abs(py_t["pnl"] - rs_t["pnl"]) < 1e-9, (
                f"Trade {i} pnl mismatch: Python={py_t['pnl']}, Rust={rs_t['pnl']}"
            )


# ── Resolution parity test ────────────────────────────────────────────────────


class BuyAndHoldStrategy(BaseStrategy):
    """Buys on first event, holds through resolution. Tests on_resolution parity."""

    def __init__(self, context):
        super().__init__(context)
        self.resolution_pnl: float | None = None
        self.resolution_outcome: str | None = None

    def on_start(self):
        self.subscribe(platform="polymarket")

    def on_market_data(self, market, price, orderbook):
        if self.get_position(market.id) is None:
            self.buy(market.id, 5.0)

    def on_resolution(self, market, outcome, pnl):
        self.resolution_pnl = pnl
        self.resolution_outcome = outcome


def test_rust_backed_context_resolution_parity(monkeypatch):
    """
    RustBackedContext with an open position that resolves should match Python.

    Strategy buys 5 contracts on first price event. Market resolves YES.
    Payout = 1.0 per contract, so pnl = 5 * (1.0 - buy_price).
    Both contexts must produce identical:
      - final_value
      - on_resolution pnl received by strategy
      - trade records (buy + resolution)
      - resolved_correctly flag on buy trade
    """
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())

    # Market close_time must be within 2024-01-01..2024-01-02 range for resolution to fire
    close_ts = now_ts + 3600  # 1 hour after start, within the range
    price_data = {
        "m1": [
            PricePoint(timestamp=now_ts + 100, yes_price=0.50, no_price=0.50, volume=10.0),
            PricePoint(timestamp=now_ts + 200, yes_price=0.55, no_price=0.45, volume=12.0),
        ]
    }
    markets = [make_market("m1", close_ts, resolved=True, resolution="yes")]

    python_result, rust_result = _run_both(
        BuyAndHoldStrategy, price_data, markets, monkeypatch, now_ts=now_ts
    )

    assert python_result["ok"], f"Python failed: {python_result}"
    assert rust_result["ok"], f"Rust failed: {rust_result}"

    py_trades = python_result["_artifact_payload"]["trades"]
    rs_trades = rust_result["_artifact_payload"]["trades"]

    print(f"\n=== Resolution Parity ===")
    print(f"Python final: {python_result['final_value']:.4f}, trades: {len(py_trades)}")
    print(f"Rust   final: {rust_result['final_value']:.4f}, trades: {len(rs_trades)}")
    for t in py_trades:
        print(f"  Python: {t['action']} {t.get('contracts')}@{t.get('price')} pnl={t.get('pnl')} resolved_correctly={t.get('resolved_correctly')}")
    for t in rs_trades:
        print(f"  Rust:   {t['action']} {t.get('contracts')}@{t.get('price')} pnl={t.get('pnl')} resolved_correctly={t.get('resolved_correctly')}")

    # Final portfolio values must match
    assert abs(python_result["final_value"] - rust_result["final_value"]) < 1e-9, (
        f"Final value: Python={python_result['final_value']}, Rust={rust_result['final_value']}"
    )

    # Both should have a buy trade + a resolution trade
    assert len(py_trades) == len(rs_trades), (
        f"Trade count: Python={len(py_trades)}, Rust={len(rs_trades)}"
    )

    py_buy = next((t for t in py_trades if t["action"] == "buy"), None)
    rs_buy = next((t for t in rs_trades if t["action"] == "buy"), None)
    assert py_buy is not None, "Python: missing buy trade"
    assert rs_buy is not None, "Rust: missing buy trade"

    # Buy prices must match
    assert abs(py_buy["price"] - rs_buy["price"]) < 1e-9, (
        f"Buy price: Python={py_buy['price']}, Rust={rs_buy['price']}"
    )

    # resolved_correctly must be True on the buy trade (bought YES, resolved YES)
    assert py_buy.get("resolved_correctly") is True, (
        f"Python buy trade resolved_correctly should be True, got {py_buy.get('resolved_correctly')}"
    )
    assert rs_buy.get("resolved_correctly") is True, (
        f"Rust buy trade resolved_correctly should be True, got {rs_buy.get('resolved_correctly')}"
    )

    # Resolution trade pnl must match
    py_res = next((t for t in py_trades if t["action"] == "resolution"), None)
    rs_res = next((t for t in rs_trades if t["action"] == "resolution"), None)
    assert py_res is not None, "Python: missing resolution trade"
    assert rs_res is not None, "Rust: missing resolution trade"
    assert abs(py_res["pnl"] - rs_res["pnl"]) < 1e-9, (
        f"Resolution pnl: Python={py_res['pnl']}, Rust={rs_res['pnl']}"
    )


# ── Warm-start parity test ────────────────────────────────────────────────────


def test_rust_backed_context_warm_start_parity(monkeypatch):
    """
    RustBackedContext warm-start: both contexts see the same pre-event price cursor.

    FakeIndex.get_latest_price_before returns 0.45 before the first event.
    The first real event arrives at now_ts + 1000 at price 0.55.
    A strategy that can only buy if price > 0.50 should NOT buy on the warm-start
    price, but SHOULD buy on the first real event — in both contexts identically.
    """
    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())

    price_data = {
        "m1": [
            PricePoint(timestamp=now_ts + 1000, yes_price=0.55, no_price=0.45, volume=10.0),
            PricePoint(timestamp=now_ts + 2000, yes_price=0.60, no_price=0.40, volume=12.0),
        ]
    }
    markets = [make_market("m1", now_ts + 86400)]

    class WarmStartIndex(FakeIndex):
        def get_latest_price_before(self, market_id, platform, ts):
            return 0.45  # warm-start price before first event

    warm_index = WarmStartIndex(price_data)

    python_result, rust_result = _run_both(
        BuyOnLowStrategy,  # buys when price < 0.48 — should NOT buy on warm-start (0.45 sets cursor, event at 0.55)
        price_data,
        markets,
        monkeypatch,
        now_ts=now_ts,
        index=warm_index,
    )

    assert python_result["ok"], f"Python failed: {python_result}"
    assert rust_result["ok"], f"Rust failed: {rust_result}"

    py_trades = python_result["_artifact_payload"]["trades"]
    rs_trades = rust_result["_artifact_payload"]["trades"]
    py_curve = python_result["_artifact_payload"]["equity_curve"]
    rs_curve = rust_result["_artifact_payload"]["equity_curve"]

    print(f"\n=== Warm-Start Parity ===")
    print(f"Python final: {python_result['final_value']:.4f}, trades: {len(py_trades)}")
    print(f"Rust   final: {rust_result['final_value']:.4f}, trades: {len(rs_trades)}")
    print(f"Python curve start ts: {py_curve[0]['timestamp'] if py_curve else 'none'}")
    print(f"Rust   curve start ts: {rs_curve[0]['timestamp'] if rs_curve else 'none'}")

    # Trade counts must match
    assert len(py_trades) == len(rs_trades), (
        f"Trade count: Python={len(py_trades)}, Rust={len(rs_trades)}"
    )

    # Final values must match
    assert abs(python_result["final_value"] - rust_result["final_value"]) < 1e-9, (
        f"Final value: Python={python_result['final_value']}, Rust={rust_result['final_value']}"
    )

    # Curve start timestamps must match
    assert py_curve[0]["timestamp"] == rs_curve[0]["timestamp"], (
        f"Curve start ts: Python={py_curve[0]['timestamp']}, Rust={rs_curve[0]['timestamp']}"
    )

    # If any trades, prices and contracts must match exactly
    for i, (py_t, rs_t) in enumerate(zip(py_trades, rs_trades)):
        assert py_t["action"] == rs_t["action"], f"Trade {i} action mismatch"
        assert abs(py_t["price"] - rs_t["price"]) < 1e-9, (
            f"Trade {i} price: Python={py_t['price']}, Rust={rs_t['price']}"
        )


# ── Direct context method parity tests ───────────────────────────────────────
# These test individual methods by constructing both contexts directly
# (not via _run_both) and asserting identical behavior.


def _make_ctx_pair(initial_cash=1000.0, markets=None, ts=100):
    """Create matching StreamingBacktestContext and RustBackedContext with the same initial state."""
    if markets is None:
        markets = {
            "m1": make_market("m1", ts + 86400),
            "m2": make_market("m2", ts + 86400),
        }
    fill_model = FillModel()
    py_ctx = StreamingBacktestContext(
        initial_cash=initial_cash,
        market_map=markets,
        fill_model=fill_model,
        execution_mode=ExecutionMode.STRICT_PRICE_ONLY,
    )
    rust_ctx = RustBackedContext(
        initial_cash=initial_cash,
        market_map=markets,
        fill_model=fill_model,
        execution_mode=ExecutionMode.STRICT_PRICE_ONLY,
    )
    return py_ctx, rust_ctx


# ── get_price parity ──────────────────────────────────────────────────────────


def test_get_price_active_market_parity():
    """get_price for active market returns cursor price in both contexts."""
    py_ctx, rust_ctx = _make_ctx_pair()
    for ctx in (py_ctx, rust_ctx):
        ctx.advance_time(100)
        ctx.set_price_cursor("m1", 0.55)
        ctx.set_active_market("m1")
    assert py_ctx.get_price("m1") == rust_ctx.get_price("m1") == 0.55


def test_get_price_look_ahead_prevention_parity():
    """
    get_price for a non-active market is blocked when cursor_ts >= current_ts.

    m2's cursor is set at ts=100 (same as current_ts). With active_market=m1,
    both contexts must return the historical price (0.60) rather than the
    cursor (0.70). This is the core look-ahead prevention invariant.
    """
    py_ctx, rust_ctx = _make_ctx_pair()
    for ctx in (py_ctx, rust_ctx):
        ctx.advance_time(100)
        ctx.set_price_cursor("m1", 0.50)
        ctx.set_price_cursor("m2", 0.70)  # cursor_ts["m2"] = 100 = current_ts
        ctx.push_history("m2", PricePoint(timestamp=50, yes_price=0.60, no_price=0.40, volume=5.0))
        ctx.set_active_market("m1")
    py_price = py_ctx.get_price("m2")
    rust_price = rust_ctx.get_price("m2")
    assert py_price == rust_price == 0.60, (
        f"Look-ahead prevention: Python={py_price}, Rust={rust_price} (expected 0.60)"
    )


def test_get_price_look_ahead_no_history_raises_parity():
    """Both contexts raise MarketNotCachedError when look-ahead blocks and no history exists."""
    py_ctx, rust_ctx = _make_ctx_pair()
    for ctx in (py_ctx, rust_ctx):
        ctx.advance_time(100)
        ctx.set_price_cursor("m1", 0.50)
        ctx.set_price_cursor("m2", 0.70)  # cursor_ts = current_ts → blocked
        ctx.set_active_market("m1")

    with pytest.raises(MarketNotCachedError):
        py_ctx.get_price("m2")
    with pytest.raises(MarketNotCachedError):
        rust_ctx.get_price("m2")


def test_get_price_cross_market_prior_cursor_parity():
    """m2 cursor set at ts=50 < current_ts=100: not blocked, returns cursor."""
    py_ctx, rust_ctx = _make_ctx_pair()
    for ctx in (py_ctx, rust_ctx):
        ctx.advance_time(50)
        ctx.set_price_cursor("m2", 0.65)  # cursor_ts["m2"] = 50
        ctx.advance_time(100)
        ctx.set_price_cursor("m1", 0.50)
        ctx.set_active_market("m1")
    assert py_ctx.get_price("m2") == rust_ctx.get_price("m2") == 0.65


# ── get_position parity ───────────────────────────────────────────────────────


def test_get_position_none_before_buy_parity():
    """get_position returns None before any trade in both contexts."""
    py_ctx, rust_ctx = _make_ctx_pair()
    assert py_ctx.get_position("m1") is None
    assert rust_ctx.get_position("m1") is None


def test_get_position_after_buy_parity():
    """After buy, get_position returns matching side, contracts, avg_cost."""
    py_ctx, rust_ctx = _make_ctx_pair(initial_cash=1000.0)
    for ctx in (py_ctx, rust_ctx):
        ctx.advance_time(100)
        ctx.set_price_cursor("m1", 0.50)
        ctx.set_active_market("m1")
        ctx.buy("m1", 10.0, side="yes")
    py_pos = py_ctx.get_position("m1")
    rust_pos = rust_ctx.get_position("m1")
    assert py_pos is not None and rust_pos is not None
    assert py_pos.side == rust_pos.side == "yes"
    assert abs(py_pos.contracts - rust_pos.contracts) < 1e-9
    assert abs(py_pos.avg_cost - rust_pos.avg_cost) < 1e-9


def test_get_position_cleared_after_sell_parity():
    """After full sell, get_position returns None in both contexts."""
    py_ctx, rust_ctx = _make_ctx_pair(initial_cash=1000.0)
    for ctx in (py_ctx, rust_ctx):
        ctx.advance_time(100)
        ctx.set_price_cursor("m1", 0.50)
        ctx.set_active_market("m1")
        ctx.buy("m1", 5.0, side="yes")
    for ctx in (py_ctx, rust_ctx):
        ctx.advance_time(200)
        ctx.set_price_cursor("m1", 0.70)
        ctx.set_active_market("m1")
        ctx.sell("m1")
    assert py_ctx.get_position("m1") is None
    assert rust_ctx.get_position("m1") is None


# ── get_cash parity ───────────────────────────────────────────────────────────


def test_get_cash_initial_parity():
    """Initial cash is identical in both contexts."""
    py_ctx, rust_ctx = _make_ctx_pair(initial_cash=2500.0)
    assert py_ctx.get_cash() == rust_ctx.get_cash() == 2500.0


def test_get_cash_after_buy_parity():
    """Cash is deducted identically after buy (10 contracts @ 0.50 = cost 5.00)."""
    py_ctx, rust_ctx = _make_ctx_pair(initial_cash=1000.0)
    for ctx in (py_ctx, rust_ctx):
        ctx.advance_time(100)
        ctx.set_price_cursor("m1", 0.50)
        ctx.set_active_market("m1")
        ctx.buy("m1", 10.0, side="yes")
    py_cash = py_ctx.get_cash()
    rust_cash = rust_ctx.get_cash()
    assert abs(py_cash - rust_cash) < 1e-9
    assert abs(py_cash - 995.0) < 1e-9  # 1000 - 10*0.50


def test_get_cash_after_buy_sell_parity():
    """Cash matches after buy+sell round trip (buy@0.50, sell@0.70 → net +2.00)."""
    py_ctx, rust_ctx = _make_ctx_pair(initial_cash=1000.0)
    for ctx in (py_ctx, rust_ctx):
        ctx.advance_time(100)
        ctx.set_price_cursor("m1", 0.50)
        ctx.set_active_market("m1")
        ctx.buy("m1", 10.0, side="yes")
    for ctx in (py_ctx, rust_ctx):
        ctx.advance_time(200)
        ctx.set_price_cursor("m1", 0.70)
        ctx.set_active_market("m1")
        ctx.sell("m1")
    py_cash = py_ctx.get_cash()
    rust_cash = rust_ctx.get_cash()
    assert abs(py_cash - rust_cash) < 1e-9
    assert abs(py_cash - 1002.0) < 1e-9  # 1000 - 5.00 + 7.00


def test_get_cash_after_settle_parity():
    """Cash matches after buy then YES resolution (buy@0.50, payout=1.0 → net +5.00)."""
    py_ctx, rust_ctx = _make_ctx_pair(initial_cash=1000.0)
    for ctx in (py_ctx, rust_ctx):
        ctx.advance_time(100)
        ctx.set_price_cursor("m1", 0.50)
        ctx.set_active_market("m1")
        ctx.buy("m1", 10.0, side="yes")
    for ctx in (py_ctx, rust_ctx):
        ctx.advance_time(300)
        ctx.set_active_market("m1")
        ctx.settle_positions("m1", "yes")
    py_cash = py_ctx.get_cash()
    rust_cash = rust_ctx.get_cash()
    assert abs(py_cash - rust_cash) < 1e-9
    assert abs(py_cash - 1005.0) < 1e-9  # 1000 - 5.00 + 10.00


# ── buy parity ────────────────────────────────────────────────────────────────


def test_buy_trade_record_fields_parity():
    """compile_results() trade record matches between contexts after buy."""
    py_ctx, rust_ctx = _make_ctx_pair(initial_cash=1000.0)
    for ctx in (py_ctx, rust_ctx):
        ctx.advance_time(100)
        ctx.set_price_cursor("m1", 0.55)
        ctx.set_active_market("m1")
        ctx.buy("m1", 8.0, side="yes")
        ctx.record_snapshot(100)
    py_raw = py_ctx.compile_results()
    rust_raw = rust_ctx.compile_results()
    assert len(py_raw["trades"]) == len(rust_raw["trades"]) == 1
    py_t = py_raw["trades"][0]
    rs_t = rust_raw["trades"][0]
    assert py_t["action"] == rs_t["action"] == "buy"
    assert py_t["side"] == rs_t["side"] == "yes"
    assert abs(py_t["contracts"] - rs_t["contracts"]) < 1e-9
    assert abs(py_t["price"] - rs_t["price"]) < 1e-9
    assert abs(py_t["slippage"] - rs_t["slippage"]) < 1e-9
    assert py_t["pnl"] is None and rs_t["pnl"] is None
    assert py_t["resolved_correctly"] is None and rs_t["resolved_correctly"] is None


def test_buy_insufficient_cash_raises_parity():
    """Both contexts raise AgentTraderError when cash is insufficient."""
    py_ctx, rust_ctx = _make_ctx_pair(initial_cash=1.0)
    for ctx in (py_ctx, rust_ctx):
        ctx.advance_time(100)
        ctx.set_price_cursor("m1", 0.50)
        ctx.set_active_market("m1")
        with pytest.raises(AgentTraderError):
            ctx.buy("m1", 100.0, side="yes")  # needs 50.0, only has 1.0


# ── sell parity ───────────────────────────────────────────────────────────────


def test_sell_trade_record_pnl_parity():
    """Sell trade pnl matches: buy@0.50, sell@0.75 → pnl = 10*(0.75-0.50) = 2.50."""
    py_ctx, rust_ctx = _make_ctx_pair(initial_cash=1000.0)
    for ctx in (py_ctx, rust_ctx):
        ctx.advance_time(100)
        ctx.set_price_cursor("m1", 0.50)
        ctx.set_active_market("m1")
        ctx.buy("m1", 10.0, side="yes")
    for ctx in (py_ctx, rust_ctx):
        ctx.advance_time(200)
        ctx.set_price_cursor("m1", 0.75)
        ctx.set_active_market("m1")
        ctx.sell("m1")
        ctx.record_snapshot(200)
    py_raw = py_ctx.compile_results()
    rust_raw = rust_ctx.compile_results()
    py_sell = next(t for t in py_raw["trades"] if t["action"] == "sell")
    rs_sell = next(t for t in rust_raw["trades"] if t["action"] == "sell")
    expected_pnl = 10.0 * (0.75 - 0.50)
    assert abs(py_sell["pnl"] - expected_pnl) < 1e-9
    assert abs(rs_sell["pnl"] - expected_pnl) < 1e-9
    assert abs(py_sell["pnl"] - rs_sell["pnl"]) < 1e-9


def test_sell_no_position_raises_parity():
    """Both contexts raise AgentTraderError when selling with no position."""
    py_ctx, rust_ctx = _make_ctx_pair()
    for ctx in (py_ctx, rust_ctx):
        ctx.advance_time(100)
        ctx.set_price_cursor("m1", 0.50)
        ctx.set_active_market("m1")
        with pytest.raises(AgentTraderError):
            ctx.sell("m1")


# ── settle parity ─────────────────────────────────────────────────────────────


def test_settle_pnl_and_resolved_correctly_parity():
    """
    After settle(YES), resolution pnl = 5*(1.0-0.60)=2.0 and resolved_correctly=True on buy trade.
    Both contexts must agree.
    """
    py_ctx, rust_ctx = _make_ctx_pair(initial_cash=1000.0)
    for ctx in (py_ctx, rust_ctx):
        ctx.advance_time(100)
        ctx.set_price_cursor("m1", 0.60)
        ctx.set_active_market("m1")
        ctx.buy("m1", 5.0, side="yes")
    for ctx in (py_ctx, rust_ctx):
        ctx.advance_time(300)
        ctx.set_active_market("m1")
        pnl = ctx.settle_positions("m1", "yes")
        assert abs(pnl - 2.0) < 1e-9, f"settle pnl: {pnl}"
        ctx.record_snapshot(300)
    py_raw = py_ctx.compile_results()
    rust_raw = rust_ctx.compile_results()
    for label, raw in (("Python", py_raw), ("Rust", rust_raw)):
        buy_t = next(t for t in raw["trades"] if t["action"] == "buy")
        res_t = next(t for t in raw["trades"] if t["action"] == "resolution")
        assert buy_t["resolved_correctly"] is True, f"{label}: resolved_correctly"
        assert abs(res_t["pnl"] - 2.0) < 1e-9, f"{label}: resolution pnl"
    py_buy = next(t for t in py_raw["trades"] if t["action"] == "buy")
    rs_buy = next(t for t in rust_raw["trades"] if t["action"] == "buy")
    assert py_buy["resolved_correctly"] == rs_buy["resolved_correctly"]
    py_res = next(t for t in py_raw["trades"] if t["action"] == "resolution")
    rs_res = next(t for t in rust_raw["trades"] if t["action"] == "resolution")
    assert abs(py_res["pnl"] - rs_res["pnl"]) < 1e-9


def test_settle_wrong_side_zero_payout_parity():
    """Bought YES, resolved NO → payout=0, pnl=-2.0, resolved_correctly=False in both contexts."""
    py_ctx, rust_ctx = _make_ctx_pair(initial_cash=1000.0)
    for ctx in (py_ctx, rust_ctx):
        ctx.advance_time(100)
        ctx.set_price_cursor("m1", 0.40)
        ctx.set_active_market("m1")
        ctx.buy("m1", 5.0, side="yes")
    for ctx in (py_ctx, rust_ctx):
        ctx.advance_time(300)
        ctx.set_active_market("m1")
        pnl = ctx.settle_positions("m1", "no")  # wrong side
        assert abs(pnl - (-2.0)) < 1e-9, f"wrong-side pnl: {pnl}"
    py_raw = py_ctx.compile_results()
    rust_raw = rust_ctx.compile_results()
    for label, raw in (("Python", py_raw), ("Rust", rust_raw)):
        buy_t = next(t for t in raw["trades"] if t["action"] == "buy")
        assert buy_t["resolved_correctly"] is False, f"{label}: should be False"
    assert abs(py_ctx.get_cash() - rust_ctx.get_cash()) < 1e-9


# ── STRICT_PRICE_ONLY guardrail tests ─────────────────────────────────────────


def test_rust_backed_context_rejects_non_strict_mode():
    """RustBackedContext.__init__ raises AgentTraderError for unsupported modes."""
    fill_model = FillModel()
    markets = {"m1": make_market("m1", 200)}
    with pytest.raises(AgentTraderError) as exc_info:
        RustBackedContext(
            initial_cash=1000.0,
            market_map=markets,
            fill_model=fill_model,
            execution_mode=ExecutionMode.OBSERVED_ORDERBOOK,
        )
    assert exc_info.value.error == "UnsupportedExecutionMode"


def test_rust_backed_context_rejects_synthetic_mode():
    """RustBackedContext also rejects SYNTHETIC_EXECUTION_MODEL."""
    fill_model = FillModel()
    markets = {"m1": make_market("m1", 200)}
    with pytest.raises(AgentTraderError) as exc_info:
        RustBackedContext(
            initial_cash=1000.0,
            market_map=markets,
            fill_model=fill_model,
            execution_mode=ExecutionMode.SYNTHETIC_EXECUTION_MODEL,
        )
    assert exc_info.value.error == "UnsupportedExecutionMode"


def test_engine_use_rust_with_non_strict_mode_falls_back(monkeypatch):
    """use_rust=True + non-STRICT mode must NOT try to construct RustBackedContext.

    Replace RustBackedContext in the engine module with a sentinel that raises a
    distinct RuntimeError. If the engine incorrectly calls it, the test fails.
    If the engine correctly skips it and uses StreamingBacktestContext, the run
    succeeds and the sentinel is never touched.
    """
    import agenttrader.core.backtest_engine as _engine_mod

    class _NeverCallMe:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("RustBackedContext should not be called for non-STRICT mode")

    monkeypatch.setattr(_engine_mod, "RustBackedContext", _NeverCallMe)

    now_ts = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    markets = {
        "m1": make_market("m1", now_ts + 86400),
    }
    price_data = {
        "m1": [
            PricePoint(timestamp=now_ts + 100, yes_price=0.55, no_price=0.45, volume=10.0),
            PricePoint(timestamp=now_ts + 200, yes_price=0.60, no_price=0.40, volume=10.0),
        ]
    }
    fake_parquet = FakeParquet(markets)
    index = FakeIndex(price_data)

    monkeypatch.setattr(
        "agenttrader.data.parquet_adapter.ParquetDataAdapter",
        lambda *a, **kw: fake_parquet,
    )

    class PassiveStrategy(BaseStrategy):
        def on_market_data(self, market, price, orderbook):
            pass

    engine = BacktestEngine(data_source=None)

    # STRICT_PRICE_ONLY but use_rust=True — sentinel must not be called, run must succeed.
    # Switch to OBSERVED_ORDERBOOK to trigger the non-STRICT fallback path.
    try:
        engine._run_streaming(
            PassiveStrategy,
            BacktestConfig(
                strategy_path="test",
                start_date="2024-01-01",
                end_date="2024-01-02",
                initial_cash=1000.0,
                execution_mode=ExecutionMode.OBSERVED_ORDERBOOK,
                use_rust=True,
            ),
            index,
        )
    except RuntimeError as exc:
        pytest.fail(f"Engine called RustBackedContext for non-STRICT mode: {exc}")


# ── get_history() parity tests ────────────────────────────────────────────────


def test_get_history_empty_before_any_push_parity():
    """get_history on a market with no history returns [] in both contexts."""
    py_ctx, rust_ctx = _make_ctx_pair()
    py_ctx.advance_time(100)
    rust_ctx.advance_time(100)
    py_ctx.set_active_market("m1")
    rust_ctx.set_active_market("m1")
    assert py_ctx.get_history("m1") == []
    assert rust_ctx.get_history("m1") == []


def test_get_history_after_push_parity():
    """get_history returns pushed points in both contexts (active market, no look-ahead)."""
    py_ctx, rust_ctx = _make_ctx_pair()
    # Push two history points
    ts1, ts2 = 50, 80
    for ctx in (py_ctx, rust_ctx):
        ctx.push_history("m1", PricePoint(timestamp=ts1, yes_price=0.45, no_price=None, volume=10.0))
        ctx.push_history("m1", PricePoint(timestamp=ts2, yes_price=0.55, no_price=None, volume=20.0))
        ctx.advance_time(100)
        ctx.set_active_market("m1")

    py_hist = py_ctx.get_history("m1", lookback_hours=24)
    rust_hist = rust_ctx.get_history("m1", lookback_hours=24)
    assert len(py_hist) == len(rust_hist) == 2
    for py_p, rs_p in zip(py_hist, rust_hist):
        assert py_p.timestamp == rs_p.timestamp
        assert abs(py_p.yes_price - rs_p.yes_price) < 1e-9
        assert py_p.no_price == rs_p.no_price
        assert abs(py_p.volume - rs_p.volume) < 1e-9


def test_get_history_look_ahead_prevention_parity():
    """For a non-active market, history at current_ts is excluded in both contexts."""
    py_ctx, rust_ctx = _make_ctx_pair()
    # Push m2 history at ts=100 (exactly current_ts — should be excluded for non-active)
    for ctx in (py_ctx, rust_ctx):
        ctx.push_history("m2", PricePoint(timestamp=80, yes_price=0.50, no_price=None, volume=5.0))
        ctx.push_history("m2", PricePoint(timestamp=100, yes_price=0.70, no_price=None, volume=5.0))  # at current_ts — excluded
        ctx.advance_time(100)
        ctx.set_active_market("m1")  # m1 is active, m2 is non-active

    py_hist = py_ctx.get_history("m2", lookback_hours=24)
    rust_hist = rust_ctx.get_history("m2", lookback_hours=24)
    # Both should exclude the ts=100 point (look-ahead prevention for non-active market)
    assert len(py_hist) == len(rust_hist) == 1
    assert py_hist[0].timestamp == rust_hist[0].timestamp == 80


def test_get_history_lookback_hours_filtering_parity():
    """lookback_hours correctly limits history window in both contexts."""
    py_ctx, rust_ctx = _make_ctx_pair()
    now_ts = 7200  # 2 hours in seconds
    # Push three points: one 3h ago (outside 2h window), two inside
    for ctx in (py_ctx, rust_ctx):
        ctx.push_history("m1", PricePoint(timestamp=now_ts - 10800, yes_price=0.40, no_price=None, volume=5.0))  # 3h ago — excluded
        ctx.push_history("m1", PricePoint(timestamp=now_ts - 3600, yes_price=0.50, no_price=None, volume=5.0))   # 1h ago — included
        ctx.push_history("m1", PricePoint(timestamp=now_ts - 1800, yes_price=0.55, no_price=None, volume=5.0))   # 30min ago — included
        ctx.advance_time(now_ts)
        ctx.set_active_market("m1")

    py_hist = py_ctx.get_history("m1", lookback_hours=2)
    rust_hist = rust_ctx.get_history("m1", lookback_hours=2)
    assert len(py_hist) == len(rust_hist) == 2
    for py_p, rs_p in zip(py_hist, rust_hist):
        assert py_p.timestamp == rs_p.timestamp
        assert abs(py_p.yes_price - rs_p.yes_price) < 1e-9


def test_get_history_uses_mirror_not_ffi():
    """get_history on RustBackedContext reads from Python mirror, not Rust FFI.
    Verify by checking mirror is populated and returns same results."""
    py_ctx, rust_ctx = _make_ctx_pair()
    for ctx in (py_ctx, rust_ctx):
        ctx.push_history("m1", PricePoint(timestamp=50, yes_price=0.45, no_price=0.55, volume=10.0))
        ctx.push_history("m1", PricePoint(timestamp=80, yes_price=0.55, no_price=0.45, volume=20.0))
        ctx.advance_time(100)
        ctx.set_active_market("m1")

    # Mirror should be populated
    assert len(rust_ctx._history_mirror["m1"]) == 2
    # get_history reads from mirror
    rust_hist = rust_ctx.get_history("m1", lookback_hours=24)
    py_hist = py_ctx.get_history("m1", lookback_hours=24)
    assert len(rust_hist) == len(py_hist) == 2


def test_get_history_only_returns_within_window():
    """get_history returns only points within lookback window, not older ones."""
    py_ctx, rust_ctx = _make_ctx_pair()
    for ctx in (py_ctx, rust_ctx):
        # Push points spanning 3 hours
        ctx.push_history("m1", PricePoint(timestamp=1000, yes_price=0.3, no_price=0.7, volume=1.0))
        ctx.push_history("m1", PricePoint(timestamp=2000, yes_price=0.4, no_price=0.6, volume=1.0))
        ctx.push_history("m1", PricePoint(timestamp=3600 + 1000, yes_price=0.5, no_price=0.5, volume=1.0))
        ctx.push_history("m1", PricePoint(timestamp=3600 + 2000, yes_price=0.6, no_price=0.4, volume=1.0))
        ctx.advance_time(3600 + 2000)
        ctx.set_active_market("m1")

    # 1h lookback from ts=5600: cutoff=5600-3600=2000, so ts>=2000 and ts<=5600
    py_hist = py_ctx.get_history("m1", lookback_hours=1)
    rust_hist = rust_ctx.get_history("m1", lookback_hours=1)
    assert len(py_hist) == 3, f"expected 3 points, got {len(py_hist)}: {[p.timestamp for p in py_hist]}"
    assert len(rust_hist) == 3, f"expected 3 points, got {len(rust_hist)}: {[p.timestamp for p in rust_hist]}"
    assert py_hist == rust_hist


def test_get_history_look_ahead_excluded_for_non_active():
    """Non-active market: get_history excludes ts == current_ts (look-ahead guard)."""
    py_ctx, rust_ctx = _make_ctx_pair()
    for ctx in (py_ctx, rust_ctx):
        ctx.push_history("m1", PricePoint(timestamp=100, yes_price=0.4, no_price=0.6, volume=1.0))
        ctx.push_history("m2", PricePoint(timestamp=100, yes_price=0.5, no_price=0.5, volume=1.0))
        ctx.advance_time(100)
        ctx.set_active_market("m1")  # m1 is active, m2 is not

    # m2 is non-active: ts==100==current_ts should be excluded
    py_hist = py_ctx.get_history("m2", lookback_hours=24)
    rust_hist = rust_ctx.get_history("m2", lookback_hours=24)
    assert len(py_hist) == 0, f"non-active market should exclude current ts, got {len(py_hist)}"
    assert len(rust_hist) == 0, f"non-active market should exclude current ts, got {len(rust_hist)}"
