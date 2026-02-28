"""Tests that MCP tools route through source_selector."""
import asyncio
import json
from unittest.mock import patch, MagicMock


def _run(coro):
    return asyncio.run(coro)


def test_get_markets_uses_source_selector():
    """get_markets should route through get_best_data_source."""
    mock_source = MagicMock()
    mock_source.get_markets.return_value = []
    with patch("agenttrader.mcp.server.is_initialized", return_value=True), \
         patch("agenttrader.mcp.server.get_best_data_source", return_value=(mock_source, "normalized-index")):
        from agenttrader.mcp.server import call_tool
        result = _run(call_tool("get_markets", {"platform": "polymarket", "limit": 5}))
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["data_source"] == "normalized-index"
    mock_source.get_markets.assert_called_once()


def test_get_history_uses_source_selector():
    """get_history should route through get_best_data_source with platform param."""
    mock_source = MagicMock()
    mock_source.get_price_history.return_value = []
    with patch("agenttrader.mcp.server.is_initialized", return_value=True), \
         patch("agenttrader.mcp.server.get_best_data_source", return_value=(mock_source, "raw-parquet")):
        from agenttrader.mcp.server import call_tool
        result = _run(call_tool("get_history", {"market_id": "test-123", "days": 7, "platform": "polymarket"}))
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["data_source"] == "raw-parquet"
    call_args = mock_source.get_price_history.call_args[0]
    assert call_args[0] == "test-123"
    assert call_args[1] == "polymarket"


def test_get_history_cache_fallback_no_platform():
    """get_history with sqlite-cache should not pass platform to get_price_history."""
    mock_source = MagicMock()
    mock_source.get_price_history.return_value = []
    mock_cache = MagicMock()
    mock_cache.get_market.return_value = MagicMock()
    with patch("agenttrader.mcp.server.is_initialized", return_value=True), \
         patch("agenttrader.mcp.server.get_best_data_source", return_value=(mock_source, "sqlite-cache")), \
         patch("agenttrader.mcp.server.DataCache", return_value=mock_cache):
        from agenttrader.mcp.server import call_tool
        result = _run(call_tool("get_history", {"market_id": "test-456"}))
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["data_source"] == "sqlite-cache"
    call_args = mock_source.get_price_history.call_args[0]
    assert call_args[0] == "test-456"
    assert isinstance(call_args[1], int)  # start_ts, not platform string
