"""Tests for market capability annotations."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import duckdb
import pytest

from agenttrader.data.index_adapter import BacktestIndexAdapter


def _run(coro):
    return asyncio.run(coro)


# --- BacktestIndexAdapter.get_market_date_ranges ---

def test_index_date_ranges_batch_query(tmp_path):
    """get_market_date_ranges returns correct min/max for queried market IDs."""
    db_path = tmp_path / "test.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("""
        CREATE TABLE market_metadata (
            market_id VARCHAR, platform VARCHAR,
            min_ts BIGINT, max_ts BIGINT, n_trades BIGINT
        )
    """)
    conn.execute("""
        INSERT INTO market_metadata VALUES
        ('m1', 'polymarket', 1717200000, 1739577600, 100),
        ('m2', 'kalshi', 1704067200, 1735689600, 50)
    """)
    conn.execute("CREATE TABLE normalized_trades (ts BIGINT, yes_price DOUBLE, volume DOUBLE, market_id VARCHAR, platform VARCHAR)")
    conn.execute("INSERT INTO normalized_trades VALUES (1, 0.5, 1.0, 'm1', 'polymarket')")
    conn.close()

    adapter = BacktestIndexAdapter(index_path=db_path)
    ranges = adapter.get_market_date_ranges(["m1", "m2", "m3"])
    adapter.close()

    assert "m1" in ranges
    assert ranges["m1"] == (1717200000, 1739577600)
    assert "m2" in ranges
    assert ranges["m2"] == (1704067200, 1735689600)
    assert "m3" not in ranges


def test_index_date_ranges_empty_list(tmp_path):
    """get_market_date_ranges with empty list returns empty dict."""
    adapter = BacktestIndexAdapter(index_path=tmp_path / "nonexistent.duckdb")
    assert adapter.get_market_date_ranges([]) == {}


# --- _compute_capabilities ---

def _make_market(mid, resolved=False):
    return SimpleNamespace(id=mid, platform=SimpleNamespace(value="polymarket"), market_type=SimpleNamespace(value="binary"), resolved=resolved)


def test_compute_capabilities_with_index():
    """Capabilities include backtest range when index is available."""
    from agenttrader.mcp.server import _compute_capabilities

    mock_adapter = MagicMock()
    mock_adapter.get_market_date_ranges.return_value = {"m1": (1717200000, 1739577600)}

    mock_cache = MagicMock()
    mock_cache.get_latest_price.return_value = None

    markets = [_make_market("m1")]
    with patch("agenttrader.mcp.server._get_cached_index_adapter", return_value=mock_adapter):
        caps = _compute_capabilities(markets, mock_cache)

    assert caps["m1"]["backtest"]["index_available"] is True
    assert caps["m1"]["backtest"]["index_start"] is not None
    assert caps["m1"]["backtest"]["index_end"] is not None


def test_compute_capabilities_without_index():
    """Backtest fields are false/null when no index available."""
    from agenttrader.mcp.server import _compute_capabilities

    mock_cache = MagicMock()
    mock_cache.get_latest_price.return_value = None

    markets = [_make_market("m1")]
    with patch("agenttrader.mcp.server._get_cached_index_adapter", return_value=None):
        caps = _compute_capabilities(markets, mock_cache)

    assert caps["m1"]["backtest"]["index_available"] is False
    assert caps["m1"]["backtest"]["index_start"] is None
    assert caps["m1"]["backtest"]["index_end"] is None


def test_compute_capabilities_cache_available():
    """history.cache_available is true when cache has data."""
    from agenttrader.mcp.server import _compute_capabilities

    mock_cache = MagicMock()
    mock_cache.get_latest_price.return_value = SimpleNamespace(timestamp=1709049600, yes_price=0.65)

    markets = [_make_market("m1")]
    with patch("agenttrader.mcp.server._get_cached_index_adapter", return_value=None):
        caps = _compute_capabilities(markets, mock_cache)

    assert caps["m1"]["history"]["cache_available"] is True
    assert caps["m1"]["history"]["last_point_timestamp"] is not None


def test_compute_capabilities_cache_empty():
    """History fields are false/null when no cache data."""
    from agenttrader.mcp.server import _compute_capabilities

    mock_cache = MagicMock()
    mock_cache.get_latest_price.return_value = None

    markets = [_make_market("m1")]
    with patch("agenttrader.mcp.server._get_cached_index_adapter", return_value=None):
        caps = _compute_capabilities(markets, mock_cache)

    assert caps["m1"]["history"]["cache_available"] is False
    assert caps["m1"]["history"]["last_point_timestamp"] is None


def test_compute_capabilities_resolved_market():
    """sync.can_attempt_live_sync is false for resolved markets."""
    from agenttrader.mcp.server import _compute_capabilities

    mock_cache = MagicMock()
    mock_cache.get_latest_price.return_value = None

    markets = [_make_market("m1", resolved=True)]
    with patch("agenttrader.mcp.server._get_cached_index_adapter", return_value=None):
        caps = _compute_capabilities(markets, mock_cache)

    assert caps["m1"]["sync"]["can_attempt_live_sync"] is False


# --- MCP integration tests ---

def test_research_markets_includes_capabilities():
    """research_markets response has capabilities on each market."""
    market = _make_market("m1")
    mock_source = MagicMock()
    mock_source.get_markets.return_value = [market]
    mock_source.get_price_history.return_value = []

    with patch("agenttrader.mcp.server.is_initialized", return_value=True), \
         patch("agenttrader.mcp.server.get_best_data_source", return_value=(mock_source, "sqlite-cache")), \
         patch("agenttrader.mcp.server.get_all_sources", return_value=[(mock_source, "sqlite-cache")]), \
         patch("agenttrader.mcp.server._get_cached_index_adapter", return_value=None):
        from agenttrader.mcp.server import call_tool
        result = _run(call_tool("research_markets", {"platform": "polymarket", "limit": 1, "days": 1}))

    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert "capabilities" in payload["markets"][0]
    caps = payload["markets"][0]["capabilities"]
    assert "backtest" in caps
    assert "history" in caps
    assert "sync" in caps


def test_get_markets_capabilities_opt_in():
    """get_markets with include_capabilities=true has capabilities."""
    market = _make_market("m1")
    mock_source = MagicMock()
    mock_source.get_markets.return_value = [market]

    with patch("agenttrader.mcp.server.is_initialized", return_value=True), \
         patch("agenttrader.mcp.server.get_best_data_source", return_value=(mock_source, "normalized-index")), \
         patch("agenttrader.mcp.server._get_cached_index_adapter", return_value=None):
        from agenttrader.mcp.server import call_tool
        result = _run(call_tool("get_markets", {"platform": "polymarket", "limit": 5, "include_capabilities": True}))

    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert "capabilities" in payload["markets"][0]


def test_get_markets_no_capabilities_by_default():
    """get_markets without flag has no capabilities field."""
    market = _make_market("m1")
    mock_source = MagicMock()
    mock_source.get_markets.return_value = [market]

    with patch("agenttrader.mcp.server.is_initialized", return_value=True), \
         patch("agenttrader.mcp.server.get_best_data_source", return_value=(mock_source, "normalized-index")):
        from agenttrader.mcp.server import call_tool
        result = _run(call_tool("get_markets", {"platform": "polymarket", "limit": 5}))

    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert "capabilities" not in payload["markets"][0]


def test_get_markets_defaults_to_active_only():
    """Broad get_markets discovery should request active markets by default."""
    market = _make_market("m1")
    mock_source = MagicMock()
    mock_source.get_markets.return_value = [market]

    with patch("agenttrader.mcp.server.is_initialized", return_value=True), \
         patch("agenttrader.mcp.server.get_best_data_source", return_value=(mock_source, "normalized-index")):
        from agenttrader.mcp.server import call_tool
        result = _run(call_tool("get_markets", {"platform": "polymarket", "limit": 5}))

    payload = json.loads(result[0].text)
    assert payload["ok"] is True
    assert mock_source.get_markets.call_args.kwargs["active_only"] is True
