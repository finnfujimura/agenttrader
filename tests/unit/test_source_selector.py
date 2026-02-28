"""Tests for data source selector."""
import sys
from unittest.mock import patch, MagicMock
import pytest


@pytest.fixture(autouse=True)
def _clean_selector_cache():
    """Remove cached module so each test gets fresh imports."""
    sys.modules.pop("agenttrader.data.source_selector", None)
    yield
    sys.modules.pop("agenttrader.data.source_selector", None)


def _make_mock_module(name, **attrs):
    """Create a mock module with given attributes."""
    mod = MagicMock()
    mod.__name__ = name
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def test_returns_index_when_available():
    """When BacktestIndexAdapter is available, prefer it."""
    mock_index_instance = MagicMock()
    mock_index_instance.is_available.return_value = True
    MockIndexClass = MagicMock(return_value=mock_index_instance)

    mock_index_mod = _make_mock_module("agenttrader.data.index_adapter", BacktestIndexAdapter=MockIndexClass)
    mock_parquet_mod = _make_mock_module("agenttrader.data.parquet_adapter", ParquetDataAdapter=MagicMock())
    mock_cache_mod = _make_mock_module("agenttrader.data.cache", DataCache=MagicMock())
    mock_db_mod = _make_mock_module("agenttrader.db", get_engine=MagicMock())

    with patch.dict(sys.modules, {
        "agenttrader.data.index_adapter": mock_index_mod,
        "agenttrader.data.parquet_adapter": mock_parquet_mod,
        "agenttrader.data.cache": mock_cache_mod,
        "agenttrader.db": mock_db_mod,
    }):
        from agenttrader.data.source_selector import get_best_data_source
        source, name = get_best_data_source()

    assert name == "normalized-index"
    assert source is mock_index_instance


def test_falls_back_to_parquet():
    """When index unavailable but parquet exists, use parquet."""
    mock_index_instance = MagicMock()
    mock_index_instance.is_available.return_value = False
    mock_parquet_instance = MagicMock()
    mock_parquet_instance.is_available.return_value = True

    mock_index_mod = _make_mock_module("agenttrader.data.index_adapter", BacktestIndexAdapter=MagicMock(return_value=mock_index_instance))
    mock_parquet_mod = _make_mock_module("agenttrader.data.parquet_adapter", ParquetDataAdapter=MagicMock(return_value=mock_parquet_instance))
    mock_cache_mod = _make_mock_module("agenttrader.data.cache", DataCache=MagicMock())
    mock_db_mod = _make_mock_module("agenttrader.db", get_engine=MagicMock())

    with patch.dict(sys.modules, {
        "agenttrader.data.index_adapter": mock_index_mod,
        "agenttrader.data.parquet_adapter": mock_parquet_mod,
        "agenttrader.data.cache": mock_cache_mod,
        "agenttrader.db": mock_db_mod,
    }):
        from agenttrader.data.source_selector import get_best_data_source
        source, name = get_best_data_source()

    assert name == "raw-parquet"
    assert source is mock_parquet_instance


def test_falls_back_to_cache():
    """When both index and parquet unavailable, use sqlite cache."""
    mock_index_instance = MagicMock()
    mock_index_instance.is_available.return_value = False
    mock_parquet_instance = MagicMock()
    mock_parquet_instance.is_available.return_value = False
    mock_cache_instance = MagicMock()
    MockCacheClass = MagicMock(return_value=mock_cache_instance)

    mock_index_mod = _make_mock_module("agenttrader.data.index_adapter", BacktestIndexAdapter=MagicMock(return_value=mock_index_instance))
    mock_parquet_mod = _make_mock_module("agenttrader.data.parquet_adapter", ParquetDataAdapter=MagicMock(return_value=mock_parquet_instance))
    mock_cache_mod = _make_mock_module("agenttrader.data.cache", DataCache=MockCacheClass)
    mock_db_mod = _make_mock_module("agenttrader.db", get_engine=MagicMock())

    with patch.dict(sys.modules, {
        "agenttrader.data.index_adapter": mock_index_mod,
        "agenttrader.data.parquet_adapter": mock_parquet_mod,
        "agenttrader.data.cache": mock_cache_mod,
        "agenttrader.db": mock_db_mod,
    }):
        from agenttrader.data.source_selector import get_best_data_source
        source, name = get_best_data_source()

    assert name == "sqlite-cache"
    assert source is mock_cache_instance
