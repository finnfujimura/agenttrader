"""Test that research_markets does not sync when indexed data is available."""
import asyncio
import json
from unittest.mock import patch, MagicMock


def _run(coro):
    return asyncio.run(coro)


def test_research_skips_sync_when_indexed():
    """research_markets should not call sync_data when using indexed data."""
    mock_source = MagicMock()
    mock_market = MagicMock()
    mock_market.id = "m1"
    mock_market.platform = MagicMock(value="polymarket")
    mock_market.market_type = MagicMock(value="binary")
    mock_market.__dict__ = {"id": "m1", "platform": mock_market.platform, "market_type": mock_market.market_type}
    mock_source.get_markets.return_value = [mock_market]
    mock_source.get_price_history.return_value = []

    with patch("agenttrader.mcp.server.is_initialized", return_value=True), \
         patch("agenttrader.mcp.server.get_best_data_source", return_value=(mock_source, "normalized-index")):
        from agenttrader.mcp.server import call_tool
        result = _run(call_tool("research_markets", {"platform": "polymarket", "limit": 1}))

    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert payload["data_source"] == "normalized-index"
    assert "sync" not in payload
    mock_source.get_markets.assert_called_once()


def test_research_includes_data_source():
    """research_markets response should include data_source field."""
    mock_source = MagicMock()
    mock_source.get_markets.return_value = []

    with patch("agenttrader.mcp.server.is_initialized", return_value=True), \
         patch("agenttrader.mcp.server.get_best_data_source", return_value=(mock_source, "raw-parquet")):
        from agenttrader.mcp.server import call_tool
        result = _run(call_tool("research_markets", {"platform": "all"}))

    payload = json.loads(result[0].text)
    assert payload["data_source"] == "raw-parquet"
