"""Tests for data source selector."""
import sys
from unittest.mock import patch, MagicMock
import pytest


@pytest.fixture(autouse=True)
def _clean_selector_cache():
    """Remove cached module so each test gets fresh imports and clear source cache."""
    mod = sys.modules.pop("agenttrader.data.source_selector", None)
    if mod and hasattr(mod, "invalidate_source_cache"):
        mod.invalidate_source_cache()
    yield
    mod = sys.modules.pop("agenttrader.data.source_selector", None)
    if mod and hasattr(mod, "invalidate_source_cache"):
        mod.invalidate_source_cache()


def _make_mock_module(name, **attrs):
    """Create a mock module with given attributes."""
    mod = MagicMock()
    mod.__name__ = name
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def test_returns_index_provider_when_available():
    """When IndexProvider is available, prefer it and return an object with get_markets."""
    mock_provider_instance = MagicMock()
    mock_provider_instance.is_available.return_value = True
    MockProviderClass = MagicMock(return_value=mock_provider_instance)

    mock_provider_mod = _make_mock_module("agenttrader.data.index_provider", IndexProvider=MockProviderClass)
    mock_parquet_mod = _make_mock_module("agenttrader.data.parquet_adapter", ParquetDataAdapter=MagicMock())
    mock_cache_mod = _make_mock_module("agenttrader.data.cache", DataCache=MagicMock())
    mock_db_mod = _make_mock_module("agenttrader.db", get_engine=MagicMock())

    with patch.dict(sys.modules, {
        "agenttrader.data.index_provider": mock_provider_mod,
        "agenttrader.data.parquet_adapter": mock_parquet_mod,
        "agenttrader.data.cache": mock_cache_mod,
        "agenttrader.db": mock_db_mod,
    }):
        from agenttrader.data.source_selector import get_best_data_source
        source, name = get_best_data_source()

    assert name == "normalized-index"
    assert source is mock_provider_instance


def test_falls_back_to_parquet():
    """When index unavailable but parquet exists, use parquet."""
    mock_provider_instance = MagicMock()
    mock_provider_instance.is_available.return_value = False
    mock_parquet_instance = MagicMock()
    mock_parquet_instance.is_available.return_value = True

    mock_provider_mod = _make_mock_module("agenttrader.data.index_provider", IndexProvider=MagicMock(return_value=mock_provider_instance))
    mock_parquet_mod = _make_mock_module("agenttrader.data.parquet_adapter", ParquetDataAdapter=MagicMock(return_value=mock_parquet_instance))
    mock_cache_mod = _make_mock_module("agenttrader.data.cache", DataCache=MagicMock())
    mock_db_mod = _make_mock_module("agenttrader.db", get_engine=MagicMock())

    with patch.dict(sys.modules, {
        "agenttrader.data.index_provider": mock_provider_mod,
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
    mock_provider_instance = MagicMock()
    mock_provider_instance.is_available.return_value = False
    mock_parquet_instance = MagicMock()
    mock_parquet_instance.is_available.return_value = False
    mock_cache_instance = MagicMock()
    MockCacheClass = MagicMock(return_value=mock_cache_instance)

    mock_provider_mod = _make_mock_module("agenttrader.data.index_provider", IndexProvider=MagicMock(return_value=mock_provider_instance))
    mock_parquet_mod = _make_mock_module("agenttrader.data.parquet_adapter", ParquetDataAdapter=MagicMock(return_value=mock_parquet_instance))
    mock_cache_mod = _make_mock_module("agenttrader.data.cache", DataCache=MockCacheClass)
    mock_db_mod = _make_mock_module("agenttrader.db", get_engine=MagicMock())

    with patch.dict(sys.modules, {
        "agenttrader.data.index_provider": mock_provider_mod,
        "agenttrader.data.parquet_adapter": mock_parquet_mod,
        "agenttrader.data.cache": mock_cache_mod,
        "agenttrader.db": mock_db_mod,
    }):
        from agenttrader.data.source_selector import get_best_data_source
        source, name = get_best_data_source()

    assert name == "sqlite-cache"
    assert source is mock_cache_instance
