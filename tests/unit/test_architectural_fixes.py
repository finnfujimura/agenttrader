"""Tests for architectural fixes: chunked OHLCV sync and backtest fallback.

1. Chunked PMXT OHLCV sync — large date ranges are split into 7-day chunks
2. Backtest fallback — index NoDataInRange falls back to legacy path
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest

from agenttrader.data.models import Market, MarketType, Platform, PricePoint


def _make_market(mid="m1", platform=Platform.POLYMARKET, category="crypto"):
    return Market(
        id=mid,
        condition_id=mid,
        platform=platform,
        title="Test market",
        category=category,
        tags=[],
        market_type=MarketType.BINARY,
        volume=1000.0,
        close_time=0,
        resolved=False,
        resolution=None,
        scalar_low=None,
        scalar_high=None,
    )


# ---------------------------------------------------------------------------
# Chunked OHLCV sync
# ---------------------------------------------------------------------------


def test_small_range_no_chunking():
    """Ranges <= 7 days should make a single fetch_ohlcv call."""
    from agenttrader.data.pmxt_client import PmxtClient

    client = PmxtClient.__new__(PmxtClient)
    mock_backend = MagicMock()
    mock_backend.fetch_ohlcv.return_value = [
        SimpleNamespace(timestamp=1000, close=0.5, volume=10),
    ]
    client._poly = mock_backend
    client._kalshi = MagicMock()

    now = int(time.time())
    start = now - 3 * 86400  # 3 days ago
    result = client.get_candlesticks_with_status("cond-1", Platform.POLYMARKET, start, now, interval=60)

    assert result["status"] == "ok"
    assert len(result["points"]) == 1
    mock_backend.fetch_ohlcv.assert_called_once()


def test_large_range_splits_into_chunks():
    """Ranges > 7 days should be split into multiple fetch_ohlcv calls."""
    from agenttrader.data.pmxt_client import PmxtClient

    client = PmxtClient.__new__(PmxtClient)
    call_ranges = []

    def fake_fetch_ohlcv(condition_id, resolution, start, end, limit):
        call_ranges.append((int(start.timestamp()), int(end.timestamp())))
        ts = int(start.timestamp()) + 3600
        return [SimpleNamespace(timestamp=ts, close=0.5, volume=10)]

    mock_backend = MagicMock()
    mock_backend.fetch_ohlcv.side_effect = fake_fetch_ohlcv
    client._poly = mock_backend
    client._kalshi = MagicMock()

    now = int(time.time())
    start = now - 20 * 86400  # 20 days ago
    result = client.get_candlesticks_with_status("cond-1", Platform.POLYMARKET, start, now, interval=60)

    assert result["status"] == "ok"
    # 20 days / 7 days per chunk = 3 chunks (7 + 7 + 6)
    assert mock_backend.fetch_ohlcv.call_count == 3
    # Each chunk should cover a different time range
    assert len(call_ranges) == 3
    assert call_ranges[0][0] == start
    assert call_ranges[-1][1] == now


def test_chunking_deduplicates_points():
    """Points at the same timestamp across chunk boundaries should be deduplicated."""
    from agenttrader.data.pmxt_client import PmxtClient

    client = PmxtClient.__new__(PmxtClient)
    overlap_ts = 1000000

    def fake_fetch_ohlcv(condition_id, resolution, start, end, limit):
        # Both chunks return a point at the overlap timestamp
        return [
            SimpleNamespace(timestamp=overlap_ts, close=0.5, volume=10),
            SimpleNamespace(timestamp=overlap_ts + 3600, close=0.6, volume=20),
        ]

    mock_backend = MagicMock()
    mock_backend.fetch_ohlcv.side_effect = fake_fetch_ohlcv
    client._poly = mock_backend
    client._kalshi = MagicMock()

    now = int(time.time())
    start = now - 15 * 86400  # 15 days, triggers chunking
    result = client.get_candlesticks_with_status("cond-1", Platform.POLYMARKET, start, now, interval=60)

    assert result["status"] == "ok"
    # Should deduplicate — unique timestamps only
    timestamps = [p.timestamp for p in result["points"]]
    assert len(timestamps) == len(set(timestamps))


def test_chunking_partial_error_still_returns_good_points():
    """If one chunk errors but others succeed, return the good points."""
    from agenttrader.data.pmxt_client import PmxtClient

    client = PmxtClient.__new__(PmxtClient)
    call_count = [0]

    def fake_fetch_ohlcv(condition_id, resolution, start, end, limit):
        call_count[0] += 1
        if call_count[0] == 2:
            raise RuntimeError("PMXT timeout")
        ts = int(start.timestamp()) + 3600
        return [SimpleNamespace(timestamp=ts, close=0.5, volume=10)]

    mock_backend = MagicMock()
    mock_backend.fetch_ohlcv.side_effect = fake_fetch_ohlcv
    client._poly = mock_backend
    client._kalshi = MagicMock()

    now = int(time.time())
    start = now - 20 * 86400
    result = client.get_candlesticks_with_status("cond-1", Platform.POLYMARKET, start, now, interval=60)

    # Got points from 2 out of 3 chunks — still "ok"
    assert result["status"] == "ok"
    assert len(result["points"]) >= 1


def test_chunking_all_errors():
    """If all chunks error, return error status."""
    from agenttrader.data.pmxt_client import PmxtClient

    client = PmxtClient.__new__(PmxtClient)

    def fake_fetch_ohlcv(condition_id, resolution, start, end, limit):
        raise RuntimeError("PMXT down")

    mock_backend = MagicMock()
    mock_backend.fetch_ohlcv.side_effect = fake_fetch_ohlcv
    client._poly = mock_backend
    client._kalshi = MagicMock()

    now = int(time.time())
    start = now - 15 * 86400
    result = client.get_candlesticks_with_status("cond-1", Platform.POLYMARKET, start, now, interval=60)

    assert result["status"] == "error"
    assert result["error"] is not None
    assert len(result["points"]) == 0


def test_empty_range_returns_empty():
    """end_time <= start_time should return empty immediately."""
    from agenttrader.data.pmxt_client import PmxtClient

    client = PmxtClient.__new__(PmxtClient)
    client._poly = MagicMock()

    result = client.get_candlesticks_with_status("cond-1", Platform.POLYMARKET, 1000, 999)
    assert result["status"] == "empty"
    assert result["points"] == []


def test_get_candlesticks_uses_chunked_path():
    """get_candlesticks (convenience wrapper) should use the chunked path."""
    from agenttrader.data.pmxt_client import PmxtClient

    client = PmxtClient.__new__(PmxtClient)
    mock_backend = MagicMock()
    mock_backend.fetch_ohlcv.return_value = [
        SimpleNamespace(timestamp=5000, close=0.7, volume=100),
    ]
    client._poly = mock_backend
    client._kalshi = MagicMock()

    now = int(time.time())
    points = client.get_candlesticks("cond-1", Platform.POLYMARKET, now - 86400, now, interval=60)
    assert len(points) == 1
    assert points[0].yes_price == 0.7


# ---------------------------------------------------------------------------
# Backtest fallback when index lacks coverage
# ---------------------------------------------------------------------------


def test_backtest_falls_back_to_legacy_on_no_data_in_range():
    """BacktestEngine.run() should fall back to legacy when index has no data for range."""
    from agenttrader.core.backtest_engine import BacktestEngine, BacktestConfig

    engine = BacktestEngine()

    class FakeIndex:
        def is_available(self):
            return True

        def get_market_ids(self, platform, start_ts, end_ts):
            return []  # No data in index for this range

        def close(self):
            pass

    legacy_result = {
        "ok": True,
        "data_source": "parquet",
        "final_value": 10500.0,
        "metrics": {},
        "trades": [],
        "equity_curve": [],
    }

    class FakeStrategy:
        def __init__(self, ctx):
            pass
        def on_start(self):
            pass

    config = BacktestConfig(
        strategy_path="test.py",
        start_date="2025-01-01",
        end_date="2025-01-31",
    )

    with patch("agenttrader.data.index_adapter.BacktestIndexAdapter", return_value=FakeIndex()), \
         patch.object(engine, "_run_legacy", return_value=legacy_result) as mock_legacy:
        result = engine.run(FakeStrategy, config)

    assert result["ok"] is True
    assert result["fallback_from"] == "normalized-index"
    assert "No normalized data" in result["fallback_reason"]
    mock_legacy.assert_called_once()


def test_backtest_no_fallback_when_index_succeeds():
    """BacktestEngine.run() should NOT fall back when streaming succeeds."""
    from agenttrader.core.backtest_engine import BacktestEngine, BacktestConfig

    engine = BacktestEngine()

    streaming_result = {
        "ok": True,
        "data_source": "normalized-index",
        "final_value": 11000.0,
    }

    class FakeIndex:
        def is_available(self):
            return True

        def close(self):
            pass

    config = BacktestConfig(
        strategy_path="test.py",
        start_date="2025-01-01",
        end_date="2025-01-31",
    )

    with patch("agenttrader.data.index_adapter.BacktestIndexAdapter", return_value=FakeIndex()), \
         patch.object(engine, "_run_streaming", return_value=streaming_result) as mock_stream, \
         patch.object(engine, "_run_legacy") as mock_legacy:
        result = engine.run(object, config)

    assert result["ok"] is True
    assert result["data_source"] == "normalized-index"
    mock_legacy.assert_not_called()


def test_backtest_falls_back_on_no_subscriptions():
    """BacktestEngine.run() should fall back on NoSubscriptions error too."""
    from agenttrader.core.backtest_engine import BacktestEngine, BacktestConfig

    engine = BacktestEngine()

    class FakeIndex:
        def is_available(self):
            return True

        def close(self):
            pass

    legacy_result = {"ok": True, "data_source": "sqlite", "final_value": 9800.0}

    config = BacktestConfig(
        strategy_path="test.py",
        start_date="2025-06-01",
        end_date="2025-06-30",
    )

    streaming_fail = {
        "ok": False,
        "error": "NoSubscriptions",
        "message": "Strategy subscribed to 0 markets with data in the requested date range.",
    }

    with patch("agenttrader.data.index_adapter.BacktestIndexAdapter", return_value=FakeIndex()), \
         patch.object(engine, "_run_streaming", return_value=streaming_fail), \
         patch.object(engine, "_run_legacy", return_value=legacy_result):
        result = engine.run(object, config)

    assert result["ok"] is True
    assert result["fallback_from"] == "normalized-index"


def test_backtest_legacy_failure_not_decorated():
    """If legacy also fails, don't add fallback_from."""
    from agenttrader.core.backtest_engine import BacktestEngine, BacktestConfig

    engine = BacktestEngine()

    class FakeIndex:
        def is_available(self):
            return True

        def close(self):
            pass

    config = BacktestConfig(
        strategy_path="test.py",
        start_date="2025-01-01",
        end_date="2025-01-31",
    )

    streaming_fail = {"ok": False, "error": "NoDataInRange", "message": "No data"}
    legacy_fail = {"ok": False, "error": "NoData", "message": "Also no data in legacy"}

    with patch("agenttrader.data.index_adapter.BacktestIndexAdapter", return_value=FakeIndex()), \
         patch.object(engine, "_run_streaming", return_value=streaming_fail), \
         patch.object(engine, "_run_legacy", return_value=legacy_fail):
        result = engine.run(object, config)

    assert result["ok"] is False
    assert "fallback_from" not in result


def test_backtest_uses_legacy_when_no_index():
    """When index is not available at all, use legacy directly."""
    from agenttrader.core.backtest_engine import BacktestEngine, BacktestConfig

    engine = BacktestEngine()

    class FakeIndex:
        def is_available(self):
            return False

    legacy_result = {"ok": True, "data_source": "parquet", "final_value": 10000.0}

    config = BacktestConfig(
        strategy_path="test.py",
        start_date="2025-01-01",
        end_date="2025-01-31",
    )

    with patch("agenttrader.data.index_adapter.BacktestIndexAdapter", return_value=FakeIndex()), \
         patch.object(engine, "_run_legacy", return_value=legacy_result):
        result = engine.run(object, config)

    assert result["ok"] is True
    assert "fallback_from" not in result
