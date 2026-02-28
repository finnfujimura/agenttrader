"""Tests for debug_data_sources MCP tool."""
import asyncio
import json
from unittest.mock import patch, MagicMock


def _run(coro):
    return asyncio.run(coro)


def test_debug_data_sources_returns_structure():
    """debug_data_sources should return sources dict with active_data_source."""
    mock_source = MagicMock()
    with patch("agenttrader.mcp.server.is_initialized", return_value=True), \
         patch("agenttrader.mcp.server.get_best_data_source", return_value=(mock_source, "sqlite-cache")), \
         patch("agenttrader.mcp.server.check_schema", return_value={"ok": True}):
        from agenttrader.mcp.server import call_tool
        result = _run(call_tool("debug_data_sources", {}))
    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert "sources" in payload
    assert "active_data_source" in payload
    assert payload["active_data_source"] == "sqlite-cache"
