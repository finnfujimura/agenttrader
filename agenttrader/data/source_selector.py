"""Shared data source selection logic.

Priority order:
1. BacktestIndexAdapter (DuckDB normalized index) -- fastest, most data
2. ParquetDataAdapter (raw parquet files) -- good data, slower queries
3. DataCache (SQLite) -- always available, requires sync
"""
from __future__ import annotations


def get_best_data_source():
    """Return (source_object, source_name_string) for the best available data source."""
    from agenttrader.data.index_provider import IndexProvider
    from agenttrader.data.parquet_adapter import ParquetDataAdapter
    from agenttrader.data.cache import DataCache
    from agenttrader.db import get_engine

    try:
        provider = IndexProvider()
        if provider.is_available():
            return provider, "normalized-index"
    except Exception:
        pass

    try:
        parquet = ParquetDataAdapter()
        if parquet.is_available():
            return parquet, "raw-parquet"
    except Exception:
        pass

    cache = DataCache(get_engine())
    return cache, "sqlite-cache"
