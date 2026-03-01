"""Tests for Repro 1 (candle identity divergence) and Repro 2 (false MarketNotFound)."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _run(coro):
    return asyncio.run(coro)


def _make_market(mid, condition_id=None, platform="polymarket", resolved=False):
    return SimpleNamespace(
        id=mid,
        condition_id=condition_id or mid,
        platform=SimpleNamespace(value=platform),
        market_type=SimpleNamespace(value="binary"),
        resolved=resolved,
        title="Test",
        category="test",
        tags=[],
        volume=100.0,
        close_time=0,
        resolution=None,
        scalar_low=None,
        scalar_high=None,
    )


# --- Repro 1: _candlestick_market_id always uses market.id ---

def test_candlestick_market_id_uses_id_not_condition_id():
    """_candlestick_market_id should always use market.id, not condition_id."""
    from agenttrader.mcp.server import _candlestick_market_id

    # Simulate parquet-sourced market where condition_id differs from id
    market = _make_market(
        mid="50887272939612765629559172143901565817521391945540156085421963433918821328137",
        condition_id="0xabc123def456",  # real Polymarket condition_id
        platform="polymarket",
    )
    result = _candlestick_market_id(market)
    assert result == "50887272939612765629559172143901565817521391945540156085421963433918821328137"
    assert result != "0xabc123def456"


def test_candlestick_market_id_consistent_across_sources():
    """Both PmxtClient-style and parquet-style markets should yield the same candle ID."""
    from agenttrader.mcp.server import _candlestick_market_id

    token_id = "50887272939612765629559172143901565817521391945540156085421963433918821328137"

    # PmxtClient-style: condition_id == id
    pmxt_market = _make_market(mid=token_id, condition_id=token_id)
    # Parquet-style: condition_id is the real condition_id
    parquet_market = _make_market(mid=token_id, condition_id="0xabc123def456")

    assert _candlestick_market_id(pmxt_market) == _candlestick_market_id(parquet_market)


def test_candlestick_market_id_kalshi():
    """Kalshi markets should also use market.id."""
    from agenttrader.mcp.server import _candlestick_market_id

    market = _make_market(mid="KXFEDDECISION-25DEC-H0", condition_id="FEDDECISION-25DEC", platform="kalshi")
    assert _candlestick_market_id(market) == "KXFEDDECISION-25DEC-H0"


# --- Repro 2: get_history returns ok with warning instead of MarketNotFound ---

def test_get_history_known_market_empty_window():
    """When market exists but no data in the requested window, return ok=true with warning."""
    mock_source = MagicMock()
    # No data in the requested 7-day window
    mock_source.get_price_history.return_value = []
    # But the market IS known with a price
    latest_price = SimpleNamespace(timestamp=1764081365, yes_price=0.42, no_price=0.58, volume=0)
    mock_source.get_latest_price.return_value = latest_price
    mock_source.get_market.return_value = MagicMock()

    with patch("agenttrader.mcp.server.is_initialized", return_value=True), \
         patch("agenttrader.mcp.server.get_all_sources", return_value=[(mock_source, "normalized-index")]):
        from agenttrader.mcp.server import call_tool
        result = _run(call_tool("get_history", {
            "market_id": "KXFEDDECISION-25DEC-H0",
            "platform": "kalshi",
            "days": 7,
        }))

    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert "warning" in payload
    assert "No history points found" in payload["warning"]
    assert payload["analytics"]["points"] == 0


def test_get_history_truly_unknown_market():
    """When market is genuinely unknown, return MarketNotFound error."""
    mock_source = MagicMock()
    mock_source.get_price_history.return_value = []
    mock_source.get_latest_price.return_value = None
    mock_source.get_market.return_value = None

    with patch("agenttrader.mcp.server.is_initialized", return_value=True), \
         patch("agenttrader.mcp.server.get_all_sources", return_value=[(mock_source, "sqlite-cache")]):
        from agenttrader.mcp.server import call_tool
        result = _run(call_tool("get_history", {
            "market_id": "totally-fake-market-123",
            "platform": "polymarket",
            "days": 7,
        }))

    payload = json.loads(result[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "MarketNotFound"


def test_get_history_with_data_in_window():
    """Normal case: data exists in window, return analytics."""
    mock_source = MagicMock()
    import time
    now = int(time.time())
    mock_source.get_price_history.return_value = [
        SimpleNamespace(timestamp=now - 3600, yes_price=0.5, no_price=0.5, volume=10),
        SimpleNamespace(timestamp=now - 1800, yes_price=0.55, no_price=0.45, volume=20),
    ]

    with patch("agenttrader.mcp.server.is_initialized", return_value=True), \
         patch("agenttrader.mcp.server.get_all_sources", return_value=[(mock_source, "normalized-index")]):
        from agenttrader.mcp.server import call_tool
        result = _run(call_tool("get_history", {
            "market_id": "test-market",
            "platform": "polymarket",
            "days": 7,
        }))

    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert "warning" not in payload
    assert payload["analytics"]["points"] == 2


# --- Repro 1 integration: sync uses consistent candle identity ---

def test_fetch_pmxt_candles_uses_market_id_not_condition_id():
    """_fetch_pmxt_candles should call get_candlesticks_with_status with market.id."""
    from agenttrader.mcp.server import _fetch_pmxt_candles

    mock_client = MagicMock()
    mock_client.get_candlesticks_with_status.return_value = {"points": [], "status": "empty", "error": None}

    token_id = "50887272939612765629559172143901565817521391945540156085421963433918821328137"
    real_condition_id = "0xabc123def456"
    market = _make_market(mid=token_id, condition_id=real_condition_id, platform="polymarket")

    _fetch_pmxt_candles(mock_client, market, 1000, 2000, 60)

    # Should be called with the token ID (market.id), not the condition_id
    call_args = mock_client.get_candlesticks_with_status.call_args[0]
    assert call_args[0] == token_id
    assert call_args[0] != real_condition_id
