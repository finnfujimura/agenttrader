"""Shared data source selection logic.

Priority order:
1. BacktestIndexAdapter (DuckDB normalized index) -- fastest, most data
2. ParquetDataAdapter (raw parquet files) -- good data, slower queries
3. DataCache (SQLite) -- always available, requires sync

Instances are cached for the process lifetime to avoid expensive
re-initialization (ParquetDataAdapter creates DuckDB views over tens of
thousands of parquet files on each __init__).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Module-level cache — populated on first call, reused thereafter.
_cached_all_sources: list | None = None
_cached_best_source: tuple | None = None


def invalidate_source_cache() -> None:
    """Reset cached sources. Call after data files change (e.g. post-sync)."""
    global _cached_all_sources, _cached_best_source
    _cached_all_sources = None
    _cached_best_source = None


def get_best_data_source():
    """Return (source_object, source_name_string) for the best available data source."""
    global _cached_best_source
    if _cached_best_source is not None:
        return _cached_best_source

    sources = get_all_sources()
    if sources:
        _cached_best_source = sources[0]
        return _cached_best_source

    # Fallback: bare cache (should always be in get_all_sources, but just in case)
    from agenttrader.data.cache import DataCache
    from agenttrader.db import get_engine
    result = DataCache(get_engine()), "sqlite-cache"
    _cached_best_source = result
    return result


def get_all_sources():
    """Return list of (source_object, source_name) for all available sources in priority order.

    IndexProvider wraps ParquetDataAdapter, so only one of them is included.
    sqlite-cache is always last.
    """
    global _cached_all_sources
    if _cached_all_sources is not None:
        return _cached_all_sources

    from agenttrader.data.index_provider import IndexProvider
    from agenttrader.data.parquet_adapter import ParquetDataAdapter
    from agenttrader.data.cache import DataCache
    from agenttrader.db import get_engine

    sources = []

    try:
        provider = IndexProvider()
        if provider.is_available():
            sources.append((provider, "normalized-index"))
    except Exception:
        logger.warning("Failed to initialize IndexProvider (DuckDB index)", exc_info=True)

    # Only add raw-parquet if IndexProvider wasn't available (it wraps parquet)
    if not sources:
        try:
            parquet = ParquetDataAdapter()
            if parquet.is_available():
                sources.append((parquet, "raw-parquet"))
        except Exception:
            logger.warning("Failed to initialize ParquetDataAdapter", exc_info=True)

    try:
        cache = DataCache(get_engine())
        sources.append((cache, "sqlite-cache"))
    except Exception:
        logger.warning("Failed to initialize SQLite cache", exc_info=True)

    _cached_all_sources = sources
    return sources
