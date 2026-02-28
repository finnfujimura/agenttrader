"""Schema health check for SQLite database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

REQUIRED_COLUMNS: dict[str, list[str]] = {
    "price_history": [
        "id",
        "market_id",
        "timestamp",
        "yes_price",
        "source",
        "granularity",
    ],
    "backtest_runs": [
        "id",
        "strategy_path",
        "start_date",
        "end_date",
        "execution_mode",
    ],
    "markets": ["id", "platform", "title"],
    "paper_portfolios": ["id", "strategy_path", "status", "cash_balance"],
}


def check_schema(db_path: Path) -> dict:
    """
    Verify all required columns exist in the SQLite database.

    Returns {"ok": True} or {"ok": False, "error": "...", "missing_columns": [...], "fix": "..."}.
    """
    if not db_path.exists():
        return {
            "ok": False,
            "error": "DatabaseNotFound",
            "message": str(db_path),
            "fix": "Run: agenttrader init",
        }

    conn = sqlite3.connect(str(db_path))
    missing: list[str] = []

    try:
        for table, required_cols in REQUIRED_COLUMNS.items():
            try:
                existing = [
                    row[1]
                    for row in conn.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                ]
                for col in required_cols:
                    if col not in existing:
                        missing.append(f"{table}.{col}")
            except sqlite3.OperationalError:
                missing.append(f"{table} (table missing)")
    finally:
        conn.close()

    if missing:
        return {
            "ok": False,
            "error": "SchemaOutOfDate",
            "missing_columns": missing,
            "fix": "Run: agenttrader init  (applies pending migrations)",
        }

    return {"ok": True}
