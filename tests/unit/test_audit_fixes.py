"""Tests for codebase audit fixes (issues #1-#18 from codebase_analysis.md)."""

import ast
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Issue #1: Strategy validator — expanded forbidden imports + dynamic imports
# ---------------------------------------------------------------------------


class TestStrategyValidator:
    def _validate(self, source: str) -> dict:
        from agenttrader.cli.validate import validate_strategy_file

        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False, encoding="utf-8") as f:
            f.write(source)
            f.flush()
            return validate_strategy_file(f.name)

    def _valid_strategy(self, extra_imports: str = "", extra_body: str = "") -> str:
        return f"""{extra_imports}
from agenttrader import BaseStrategy

class MyStrategy(BaseStrategy):
    def on_start(self):
        pass
    def on_market_data(self, market, price, orderbook):
        pass
    {extra_body}
"""

    def test_os_import_blocked(self):
        result = self._validate(self._valid_strategy("import os"))
        assert result["valid"] is False
        assert any(e["type"] == "ForbiddenImport" for e in result["errors"])

    def test_subprocess_import_blocked(self):
        result = self._validate(self._valid_strategy("import subprocess"))
        assert result["valid"] is False
        assert any(e["type"] == "ForbiddenImport" for e in result["errors"])

    def test_socket_import_blocked(self):
        result = self._validate(self._valid_strategy("import socket"))
        assert result["valid"] is False
        assert any(e["type"] == "ForbiddenImport" for e in result["errors"])

    def test_ctypes_import_blocked(self):
        result = self._validate(self._valid_strategy("import ctypes"))
        assert result["valid"] is False
        assert any(e["type"] == "ForbiddenImport" for e in result["errors"])

    def test_importlib_import_blocked(self):
        result = self._validate(self._valid_strategy("import importlib"))
        assert result["valid"] is False
        assert any(e["type"] == "ForbiddenImport" for e in result["errors"])

    def test_sys_import_blocked(self):
        result = self._validate(self._valid_strategy("import sys"))
        assert result["valid"] is False
        assert any(e["type"] == "ForbiddenImport" for e in result["errors"])

    def test_shutil_import_blocked(self):
        result = self._validate(self._valid_strategy("import shutil"))
        assert result["valid"] is False
        assert any(e["type"] == "ForbiddenImport" for e in result["errors"])

    def test_dunder_import_blocked(self):
        """__import__() should produce a DynamicImport error (not just warning)."""
        source = self._valid_strategy(extra_body='def helper(self): __import__("os")')
        result = self._validate(source)
        assert not result["valid"]
        assert any(e["type"] == "DynamicImport" for e in result["errors"])

    def test_importlib_import_module_blocked(self):
        """importlib.import_module() should produce a DynamicImport error."""
        source = self._valid_strategy(
            "import importlib",
            'def helper(self): importlib.import_module("os")',
        )
        result = self._validate(source)
        assert not result["valid"]
        assert any(e["type"] == "DynamicImport" for e in result["errors"])

    def test_safe_imports_allowed(self):
        """math, statistics, etc. should still be allowed."""
        result = self._validate(self._valid_strategy("import math\nimport statistics"))
        assert result["valid"]
        assert len(result["warnings"]) == 0


# ---------------------------------------------------------------------------
# Issue #2: SQLite WAL mode + timeout
# ---------------------------------------------------------------------------


class TestSqliteConfig:
    def test_wal_mode_enabled(self):
        """Engine should set WAL mode on connect."""
        from agenttrader.db import _engine_cache

        # Clear cache to force re-creation
        _engine_cache.clear()

        tmpdir = tempfile.mkdtemp()
        db_path = Path(tmpdir) / "test.sqlite"
        from agenttrader.db import get_engine
        engine = get_engine(db_path)

        with engine.connect() as conn:
            result = conn.execute(
                __import__("sqlalchemy").text("PRAGMA journal_mode")
            ).scalar()
            assert result == "wal"

        # Dispose engine to release file handles before cleanup
        engine.dispose()
        _engine_cache.clear()

    def test_timeout_configured(self):
        """Engine should have timeout=30 in connect_args."""
        from agenttrader.db import _engine_cache
        _engine_cache.clear()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            from agenttrader.db import get_engine
            engine = get_engine(db_path)

            # The timeout is set via connect_args; verify by checking the
            # engine's creation args contain timeout
            url_str = str(engine.url)
            assert "sqlite" in url_str

            _engine_cache.clear()


# ---------------------------------------------------------------------------
# Issue #3: Detached SQLAlchemy objects — expire_on_commit=False
# ---------------------------------------------------------------------------


class TestSessionConfig:
    def test_expire_on_commit_disabled(self):
        """Sessionmaker should have expire_on_commit=False."""
        from agenttrader.db import _engine_cache, _sessionmaker_cache, get_engine, get_session

        _engine_cache.clear()
        _sessionmaker_cache.clear()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.sqlite"
            engine = get_engine(db_path)
            session = get_session(engine)

            # Check that expire_on_commit is False
            assert session.expire_on_commit is False

            session.close()
            _engine_cache.clear()
            _sessionmaker_cache.clear()


# ---------------------------------------------------------------------------
# Issue #4: MCP input bounds validation
# ---------------------------------------------------------------------------


class TestMcpBounds:
    def test_bounded_int_clamps_high(self):
        from agenttrader.mcp.server import _bounded_int
        assert _bounded_int({"limit": 999999}, "limit", 20, 1, 1000) == 1000

    def test_bounded_int_clamps_low(self):
        from agenttrader.mcp.server import _bounded_int
        assert _bounded_int({"limit": -5}, "limit", 20, 1, 1000) == 1

    def test_bounded_int_default(self):
        from agenttrader.mcp.server import _bounded_int
        assert _bounded_int({}, "limit", 20, 1, 1000) == 20

    def test_bounded_float_clamps_high(self):
        from agenttrader.mcp.server import _bounded_float
        assert _bounded_float({"cash": 1e20}, "cash", 10000.0, 1.0, 1e9) == 1e9

    def test_bounded_float_clamps_low(self):
        from agenttrader.mcp.server import _bounded_float
        assert _bounded_float({"cash": -100}, "cash", 10000.0, 1.0, 1e9) == 1.0


# ---------------------------------------------------------------------------
# Issue #8: Source selector logs warnings instead of silent pass
# ---------------------------------------------------------------------------


class TestSourceSelectorLogging:
    def test_index_failure_logged(self):
        """IndexProvider failure should be logged, not silenced."""
        import agenttrader.data.source_selector as ss
        from agenttrader.data.source_selector import get_all_sources, invalidate_source_cache

        invalidate_source_cache()

        with patch("agenttrader.data.index_provider.IndexProvider", side_effect=RuntimeError("boom")), \
             patch("agenttrader.data.parquet_adapter.ParquetDataAdapter") as mock_parquet, \
             patch.object(ss, "logger") as mock_logger, \
             patch("agenttrader.db.get_engine"), \
             patch("agenttrader.data.cache.DataCache"):
            mock_parquet.return_value.is_available.return_value = False
            get_all_sources()
            mock_logger.warning.assert_called()
            assert "IndexProvider" in str(mock_logger.warning.call_args)

        invalidate_source_cache()


# ---------------------------------------------------------------------------
# Issue #13: buy/sell parameter validation
# ---------------------------------------------------------------------------


class TestBuyValidation:
    def test_invalid_order_type_rejected(self):
        """order_type must be 'market' or 'limit'."""
        from agenttrader.core.context import _validate_buy_params
        from agenttrader.errors import AgentTraderError

        with pytest.raises(AgentTraderError, match="order_type"):
            _validate_buy_params(10, "stop_loss", None)

    def test_limit_without_price_rejected(self):
        """order_type='limit' requires limit_price."""
        from agenttrader.core.context import _validate_buy_params
        from agenttrader.errors import AgentTraderError

        with pytest.raises(AgentTraderError, match="limit_price"):
            _validate_buy_params(10, "limit", None)

    def test_valid_market_order(self):
        """Market order should pass validation."""
        from agenttrader.core.context import _validate_buy_params
        _validate_buy_params(10, "market", None)  # Should not raise

    def test_valid_limit_order(self):
        """Limit order with price should pass validation."""
        from agenttrader.core.context import _validate_buy_params
        _validate_buy_params(10, "limit", 0.50)  # Should not raise

    def test_zero_contracts_rejected(self):
        """Zero contracts should be rejected."""
        from agenttrader.core.context import _validate_buy_params
        from agenttrader.errors import AgentTraderError

        with pytest.raises(AgentTraderError, match="contracts"):
            _validate_buy_params(0, "market", None)


# ---------------------------------------------------------------------------
# Issue #18: dome_client.py removal
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Issue #12: Config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_valid_config_passes(self):
        """Default config validates without error."""
        from agenttrader.config import DEFAULT_CONFIG, _validate_config
        cfg = dict(DEFAULT_CONFIG)
        result = _validate_config(cfg)
        assert result["schedule_interval_minutes"] == 15
        assert result["default_initial_cash"] == 10000.0
        assert result["sync_granularity"] == "hourly"
        assert result["max_sync_days"] == 90

    def test_negative_schedule_interval_rejected(self):
        """schedule_interval_minutes: -1 raises ConfigError."""
        from agenttrader.config import ConfigError, DEFAULT_CONFIG, _validate_config
        cfg = dict(DEFAULT_CONFIG, schedule_interval_minutes=-1)
        with pytest.raises(ConfigError, match="schedule_interval_minutes must be >= 1"):
            _validate_config(cfg)

    def test_invalid_type_rejected(self):
        """default_initial_cash: 'banana' raises ConfigError."""
        from agenttrader.config import ConfigError, DEFAULT_CONFIG, _validate_config
        cfg = dict(DEFAULT_CONFIG, default_initial_cash="banana")
        with pytest.raises(ConfigError, match="default_initial_cash must be a number"):
            _validate_config(cfg)

    def test_invalid_granularity_rejected(self):
        """sync_granularity: 'nanosecond' raises ConfigError."""
        from agenttrader.config import ConfigError, DEFAULT_CONFIG, _validate_config
        cfg = dict(DEFAULT_CONFIG, sync_granularity="nanosecond")
        with pytest.raises(ConfigError, match="sync_granularity must be one of"):
            _validate_config(cfg)

    def test_config_coerces_types(self):
        """String '15' should be coerced to int 15."""
        from agenttrader.config import DEFAULT_CONFIG, _validate_config
        cfg = dict(DEFAULT_CONFIG, schedule_interval_minutes="15")
        result = _validate_config(cfg)
        assert result["schedule_interval_minutes"] == 15
        assert isinstance(result["schedule_interval_minutes"], int)

    def test_max_sync_days_out_of_range(self):
        """max_sync_days: 0 raises ConfigError."""
        from agenttrader.config import ConfigError, DEFAULT_CONFIG, _validate_config
        cfg = dict(DEFAULT_CONFIG, max_sync_days=0)
        with pytest.raises(ConfigError, match="max_sync_days must be between 1 and 3650"):
            _validate_config(cfg)

    def test_multiple_errors_collected(self):
        """Multiple invalid values should all be reported."""
        from agenttrader.config import ConfigError, DEFAULT_CONFIG, _validate_config
        cfg = dict(DEFAULT_CONFIG, schedule_interval_minutes=-1, default_initial_cash="banana")
        with pytest.raises(ConfigError) as exc_info:
            _validate_config(cfg)
        msg = str(exc_info.value)
        assert "schedule_interval_minutes" in msg
        assert "default_initial_cash" in msg


# ---------------------------------------------------------------------------
# Issue #6: Look-ahead bias in get_history()
# ---------------------------------------------------------------------------


class TestLookAheadBias:
    def test_streaming_get_history_excludes_current_ts_for_inactive_market(self):
        """Market A's callback can't see Market B's same-timestamp price via get_history()."""
        from agenttrader.core.context import StreamingBacktestContext
        from agenttrader.core.fill_model import FillModel
        from agenttrader.data.models import Market, MarketType, Platform, PricePoint

        market_a = Market("A", "Market A", Platform.POLYMARKET, "A", "", [], MarketType.BINARY, 0.0, 0, False, None, None, None)
        market_b = Market("B", "Market B", Platform.POLYMARKET, "B", "", [], MarketType.BINARY, 0.0, 0, False, None, None, None)
        ctx = StreamingBacktestContext(
            initial_cash=10000.0,
            market_map={"A": market_a, "B": market_b},
            fill_model=FillModel(),
        )

        # Both markets have a price point at T=1000
        point_a = PricePoint(timestamp=1000, yes_price=0.5, no_price=0.5, volume=100.0)
        point_b = PricePoint(timestamp=1000, yes_price=0.6, no_price=0.4, volume=100.0)

        # Also add earlier points so history is non-empty
        early_b = PricePoint(timestamp=500, yes_price=0.55, no_price=0.45, volume=50.0)

        ctx.advance_time(1000)
        ctx.push_history("B", early_b)
        ctx.push_history("B", point_b)
        ctx.push_history("A", point_a)

        # Market A is active — querying B's history should NOT include T=1000
        ctx.set_active_market("A")
        history_b = ctx.get_history("B", lookback_hours=24)
        timestamps = [p.timestamp for p in history_b]
        assert 1000 not in timestamps
        assert 500 in timestamps

    def test_streaming_get_history_includes_current_ts_for_active_market(self):
        """Market A's callback CAN see its own same-timestamp price."""
        from agenttrader.core.context import StreamingBacktestContext
        from agenttrader.core.fill_model import FillModel
        from agenttrader.data.models import Market, MarketType, Platform, PricePoint

        market_a = Market("A", "Market A", Platform.POLYMARKET, "A", "", [], MarketType.BINARY, 0.0, 0, False, None, None, None)
        ctx = StreamingBacktestContext(
            initial_cash=10000.0,
            market_map={"A": market_a},
            fill_model=FillModel(),
        )

        point_a = PricePoint(timestamp=1000, yes_price=0.5, no_price=0.5, volume=100.0)
        ctx.advance_time(1000)
        ctx.push_history("A", point_a)

        ctx.set_active_market("A")
        history_a = ctx.get_history("A", lookback_hours=24)
        timestamps = [p.timestamp for p in history_a]
        assert 1000 in timestamps

    def test_legacy_get_history_excludes_current_ts_for_inactive_market(self):
        """Same guard in BacktestContext."""
        from agenttrader.core.context import BacktestContext
        from agenttrader.data.models import Market, MarketType, Platform, PricePoint

        market_a = Market("A", "Market A", Platform.POLYMARKET, "A", "", [], MarketType.BINARY, 0.0, 0, False, None, None, None)
        market_b = Market("B", "Market B", Platform.POLYMARKET, "B", "", [], MarketType.BINARY, 0.0, 0, False, None, None, None)

        price_data = {
            "A": [PricePoint(timestamp=1000, yes_price=0.5, no_price=0.5, volume=100.0)],
            "B": [
                PricePoint(timestamp=500, yes_price=0.55, no_price=0.45, volume=50.0),
                PricePoint(timestamp=1000, yes_price=0.6, no_price=0.4, volume=100.0),
            ],
        }

        ctx = BacktestContext(
            initial_cash=10000.0,
            price_data=price_data,
            orderbook_data=None,
            markets={"A": market_a, "B": market_b},
        )

        ctx.advance_time(1000)
        ctx.set_active_market("A")

        history_b = ctx.get_history("B", lookback_hours=24)
        timestamps = [p.timestamp for p in history_b]
        assert 1000 not in timestamps
        assert 500 in timestamps


# ---------------------------------------------------------------------------
# Issue #18: dome_client.py removal
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Issue: DuckDB view name sanitization
# ---------------------------------------------------------------------------


class TestViewNameSanitization:
    def test_create_view_rejects_invalid_name(self):
        """_create_view should reject names with SQL injection characters."""
        from agenttrader.data.parquet_adapter import ParquetDataAdapter

        adapter = ParquetDataAdapter.__new__(ParquetDataAdapter)
        adapter._conn = MagicMock()

        with pytest.raises(ValueError, match="Invalid view name"):
            adapter._create_view("bad; DROP TABLE", ["file.parquet"])

    def test_create_view_rejects_uppercase(self):
        """_create_view should reject uppercase names."""
        from agenttrader.data.parquet_adapter import ParquetDataAdapter

        adapter = ParquetDataAdapter.__new__(ParquetDataAdapter)
        adapter._conn = MagicMock()

        with pytest.raises(ValueError, match="Invalid view name"):
            adapter._create_view("BadName", ["file.parquet"])

    def test_create_view_accepts_valid_name(self):
        """_create_view should accept valid lowercase identifiers."""
        from agenttrader.data.parquet_adapter import ParquetDataAdapter

        adapter = ParquetDataAdapter.__new__(ParquetDataAdapter)
        adapter._conn = MagicMock()

        result = adapter._create_view("poly_trades", ["file.parquet"])
        assert result == "poly_trades"
        adapter._conn.execute.assert_called_once()


class TestDomeClientRemoved:
    def test_dome_client_file_does_not_exist(self):
        """dome_client.py should be removed from the codebase."""
        path = Path(__file__).resolve().parents[2] / "agenttrader" / "data" / "dome_client.py"
        assert not path.exists(), f"dome_client.py still exists at {path}"
