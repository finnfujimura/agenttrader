# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

from pathlib import Path

import click
import duckdb


INDEX_PATH = Path.home() / ".agenttrader" / "backtest_index.duckdb"
DATA_DIR = Path.home() / ".agenttrader" / "data"


def _safe_parquet_list(directory: Path) -> list[str]:
    """Return sorted parquet paths recursively, excluding AppleDouble (._*) files."""
    if not directory.exists():
        return []
    return sorted(str(f) for f in directory.rglob("*.parquet") if not f.name.startswith("._"))


def _resolve_data_dir(data_dir: Path | None) -> Path:
    """
    Resolve dataset location.
    Preference order:
      1) explicit data_dir argument (if provided)
      2) local ./data (if exists)
      3) ~/.agenttrader/data
    """
    if data_dir is not None:
        return data_dir
    local_data_dir = Path.cwd() / "data"
    if local_data_dir.exists():
        return local_data_dir
    return DATA_DIR


def _parquet_read_expr(files: list[str]) -> str:
    """Format a list of file paths for DuckDB read_parquet([...])."""
    quoted = ", ".join("'" + f.replace("'", "''") + "'" for f in files)
    return f"read_parquet([{quoted}])"


def _build_polymarket_normalized(conn: duckdb.DuckDBPyConnection, data_dir: Path, stats: dict) -> None:
    poly_trades = _safe_parquet_list(data_dir / "polymarket" / "trades")
    poly_markets = _safe_parquet_list(data_dir / "polymarket" / "markets")
    poly_blocks = _safe_parquet_list(data_dir / "polymarket" / "blocks")

    if not poly_trades or not poly_markets or not poly_blocks:
        click.echo("  Polymarket: raw files missing, skipping.")
        stats["polymarket_trades"] = 0
        return

    conn.execute(f"CREATE OR REPLACE VIEW raw_poly_trades AS SELECT * FROM {_parquet_read_expr(poly_trades)}")
    conn.execute(f"CREATE OR REPLACE VIEW raw_poly_markets AS SELECT * FROM {_parquet_read_expr(poly_markets)}")
    conn.execute(f"CREATE OR REPLACE VIEW raw_poly_blocks AS SELECT * FROM {_parquet_read_expr(poly_blocks)}")

    conn.execute(
        """
        INSERT INTO normalized_trades
        WITH block_times AS (
            SELECT
                block_number,
                CASE
                    WHEN TRY_CAST(timestamp AS BIGINT) IS NOT NULL
                        THEN TRY_CAST(timestamp AS BIGINT)
                    WHEN TRY_CAST(timestamp AS TIMESTAMP) IS NOT NULL
                        THEN CAST(EPOCH(TRY_CAST(timestamp AS TIMESTAMP)) AS BIGINT)
                    ELSE NULL
                END AS ts
            FROM raw_poly_blocks
        ),
        market_tokens AS (
            SELECT
                json_extract_string(clob_token_ids, '$[0]') AS market_id,
                json_extract_string(clob_token_ids, '$[0]') AS yes_token_id
            FROM raw_poly_markets
            WHERE json_extract_string(clob_token_ids, '$[0]') IS NOT NULL
        ),
        trades_with_price AS (
            SELECT
                t.block_number,
                m.market_id,
                CASE
                    WHEN t.taker_asset_id = m.yes_token_id
                        THEN CAST(t.taker_amount AS DOUBLE) /
                             NULLIF(CAST(t.maker_amount + t.taker_amount AS DOUBLE), 0)
                    ELSE
                        CAST(t.maker_amount AS DOUBLE) /
                        NULLIF(CAST(t.maker_amount + t.taker_amount AS DOUBLE), 0)
                END AS yes_price,
                CAST(t.taker_amount + t.maker_amount AS DOUBLE) / 1e6 AS volume
            FROM raw_poly_trades t
            JOIN market_tokens m
              ON t.taker_asset_id = m.yes_token_id
              OR t.maker_asset_id = m.yes_token_id
        )
        SELECT
            twp.market_id,
            'polymarket' AS platform,
            bt.ts AS ts,
            twp.yes_price,
            twp.volume
        FROM trades_with_price twp
        JOIN block_times bt ON twp.block_number = bt.block_number
        WHERE bt.ts IS NOT NULL
          AND twp.market_id IS NOT NULL
          AND twp.yes_price BETWEEN 0.001 AND 0.999
        """
    )

    count = conn.execute("SELECT COUNT(*) FROM normalized_trades WHERE platform = 'polymarket'").fetchone()[0]
    stats["polymarket_trades"] = int(count)
    click.echo(f"  Polymarket: {count:,} trades normalized")


def _build_kalshi_normalized(conn: duckdb.DuckDBPyConnection, data_dir: Path, stats: dict) -> None:
    kalshi_trades = _safe_parquet_list(data_dir / "kalshi" / "trades")
    if not kalshi_trades:
        click.echo("  Kalshi: raw files missing, skipping.")
        stats["kalshi_trades"] = 0
        return

    conn.execute(f"CREATE OR REPLACE VIEW raw_kalshi_trades AS SELECT * FROM {_parquet_read_expr(kalshi_trades)}")
    conn.execute(
        """
        INSERT INTO normalized_trades
        SELECT
            ticker AS market_id,
            'kalshi' AS platform,
            CAST(EPOCH(created_time) AS BIGINT) AS ts,
            yes_price / 100.0 AS yes_price,
            CAST(count AS DOUBLE) AS volume
        FROM raw_kalshi_trades
        WHERE yes_price BETWEEN 1 AND 99
          AND created_time IS NOT NULL
        """
    )

    count = conn.execute("SELECT COUNT(*) FROM normalized_trades WHERE platform = 'kalshi'").fetchone()[0]
    stats["kalshi_trades"] = int(count)
    click.echo(f"  Kalshi: {count:,} trades normalized")


def _build_metadata_table(conn: duckdb.DuckDBPyConnection, stats: dict) -> None:
    conn.execute(
        """
        CREATE OR REPLACE TABLE market_metadata AS
        SELECT
            market_id,
            platform,
            MIN(ts)        AS min_ts,
            MAX(ts)        AS max_ts,
            COUNT(*)       AS n_trades,
            AVG(yes_price) AS avg_price
        FROM normalized_trades
        GROUP BY market_id, platform
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_meta_platform ON market_metadata(platform)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_market_ts ON normalized_trades(market_id, ts)")

    market_count = conn.execute("SELECT COUNT(*) FROM market_metadata").fetchone()[0]
    stats["markets_indexed"] = int(market_count)
    click.echo(f"  Metadata: {market_count:,} markets indexed")


def build_index(force: bool = False, data_dir: Path | None = None, index_path: Path | None = None) -> dict:
    data_dir = _resolve_data_dir(data_dir)
    index_path = index_path or INDEX_PATH

    if index_path.exists() and not force:
        return {
            "ok": True,
            "skipped": True,
            "message": "Index already exists. Use --force to rebuild.",
            "path": str(index_path),
        }

    file_count = len(_safe_parquet_list(data_dir))
    if file_count == 0:
        return {
            "ok": False,
            "error": "DatasetNotFound",
            "message": (
                f"Raw parquet files not found in {data_dir} "
                f"(found {file_count} .parquet files). "
                "Run: agenttrader dataset download or place parquet files under ./data"
            ),
            "data_dir": str(data_dir),
            "parquet_files_found": file_count,
        }

    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.unlink(missing_ok=True)

    conn = duckdb.connect(str(index_path))
    stats: dict[str, int] = {}
    try:
        conn.execute(
            """
            CREATE TABLE normalized_trades (
                market_id TEXT,
                platform TEXT,
                ts BIGINT,
                yes_price DOUBLE,
                volume DOUBLE
            )
            """
        )
        _build_polymarket_normalized(conn, data_dir, stats)
        _build_kalshi_normalized(conn, data_dir, stats)
        _build_metadata_table(conn, stats)
    except Exception:
        conn.close()
        index_path.unlink(missing_ok=True)
        raise
    conn.close()

    return {
        "ok": True,
        "skipped": False,
        "stats": stats,
        "path": str(index_path),
        "data_dir": str(data_dir),
    }
