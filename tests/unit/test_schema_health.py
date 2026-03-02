"""Tests for schema health check."""

import sqlite3
from pathlib import Path

from agenttrader.db.health import check_schema


def test_healthy_schema(tmp_path: Path) -> None:
    """A database with all required columns passes."""
    db = tmp_path / "test.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE price_history (
        id INTEGER PRIMARY KEY, market_id TEXT, platform TEXT,
        timestamp INTEGER, yes_price REAL, source TEXT, granularity TEXT
    )""")
    conn.execute("""CREATE TABLE backtest_runs (
        id TEXT PRIMARY KEY, strategy_path TEXT, strategy_hash TEXT,
        start_date TEXT, end_date TEXT, initial_cash REAL, status TEXT,
        created_at INTEGER, execution_mode TEXT
    )""")
    conn.execute("""CREATE TABLE markets (
        id TEXT PRIMARY KEY, market_id TEXT, platform TEXT, title TEXT
    )""")
    conn.execute("""CREATE TABLE paper_portfolios (
        id TEXT PRIMARY KEY, strategy_path TEXT, strategy_hash TEXT,
        initial_cash REAL, cash_balance REAL, status TEXT
    )""")
    conn.commit()
    conn.close()

    result = check_schema(db)
    assert result["ok"] is True


def test_missing_source_column(tmp_path: Path) -> None:
    """Detects missing price_history.source."""
    db = tmp_path / "test.sqlite"
    conn = sqlite3.connect(str(db))
    conn.execute("""CREATE TABLE price_history (
        id INTEGER PRIMARY KEY, market_id TEXT, platform TEXT,
        timestamp INTEGER, yes_price REAL
    )""")
    conn.execute("""CREATE TABLE backtest_runs (
        id TEXT PRIMARY KEY, strategy_path TEXT, strategy_hash TEXT,
        start_date TEXT, end_date TEXT, initial_cash REAL, status TEXT,
        created_at INTEGER, execution_mode TEXT
    )""")
    conn.execute(
        "CREATE TABLE markets (id TEXT PRIMARY KEY, market_id TEXT, platform TEXT, title TEXT)"
    )
    conn.execute("""CREATE TABLE paper_portfolios (
        id TEXT PRIMARY KEY, strategy_path TEXT, strategy_hash TEXT,
        initial_cash REAL, cash_balance REAL, status TEXT
    )""")
    conn.commit()
    conn.close()

    result = check_schema(db)
    assert result["ok"] is False
    assert result["error"] == "SchemaOutOfDate"
    assert "price_history.source" in result["missing_columns"]
    assert "fix" in result


