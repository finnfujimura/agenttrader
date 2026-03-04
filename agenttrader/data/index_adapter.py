# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

from pathlib import Path

import duckdb

from agenttrader.config import BACKTEST_INDEX_PATH
from agenttrader.data.models import PricePoint

INDEX_PATH = BACKTEST_INDEX_PATH


class BacktestIndexAdapter:
    """
    Read-only interface to the normalized DuckDB backtest index.
    Built by 'agenttrader dataset build-index'.
    """

    def __init__(self, index_path: Path | None = None):
        self._path = index_path or INDEX_PATH
        if self._path.exists():
            self._conn = duckdb.connect(str(self._path), read_only=True)
        else:
            self._conn = None

    def is_available(self) -> bool:
        if self._conn is None:
            return False
        try:
            self._conn.execute("SELECT 1 FROM normalized_trades LIMIT 1")
            self._conn.execute("SELECT 1 FROM market_metadata LIMIT 1")
            return True
        except Exception:
            return False

    def get_market_ids(self, platform: str = "all", start_ts: int | None = None, end_ts: int | None = None) -> list[tuple[str, str]]:
        rows = self.get_market_ids_with_counts(platform=platform, start_ts=start_ts, end_ts=end_ts)
        return [(market_id, market_platform) for market_id, market_platform, _ in rows]

    def get_market_rows(
        self,
        platform: str = "all",
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> list[tuple[str, str, int, int, int]]:
        if self._conn is None:
            return []

        conditions: list[str] = []
        params: list[object] = []
        if platform != "all":
            conditions.append("platform = ?")
            params.append(platform)
        if start_ts is not None:
            conditions.append("max_ts >= ?")
            params.append(int(start_ts))
        if end_ts is not None:
            conditions.append("min_ts <= ?")
            params.append(int(end_ts))

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._conn.execute(
            f"""
            SELECT market_id, platform, n_trades, min_ts, max_ts
            FROM market_metadata
            {where}
            ORDER BY n_trades DESC
            """,
            params,
        ).fetchall()
        return [
            (str(market_id), str(market_platform), int(n_trades), int(min_ts), int(max_ts))
            for market_id, market_platform, n_trades, min_ts, max_ts in rows
        ]

    def get_market_ids_with_counts(
        self,
        platform: str = "all",
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> list[tuple[str, str, int]]:
        rows = self.get_market_rows(platform=platform, start_ts=start_ts, end_ts=end_ts)
        return [(market_id, market_platform, n_trades) for market_id, market_platform, n_trades, _min_ts, _max_ts in rows]

    def get_market_date_ranges(self, market_ids: list[str]) -> dict[str, tuple[int, int]]:
        """Batch query min_ts/max_ts for a list of market IDs from market_metadata."""
        if self._conn is None or not market_ids:
            return {}
        placeholders = ", ".join("?" for _ in market_ids)
        rows = self._conn.execute(
            f"""
            SELECT market_id, min_ts, max_ts
            FROM market_metadata
            WHERE market_id IN ({placeholders})
            """,
            market_ids,
        ).fetchall()
        return {str(r[0]): (int(r[1]), int(r[2])) for r in rows}

    def stream_market_history(
        self,
        market_id: str,
        platform: str,
        start_ts: int,
        end_ts: int,
        batch_size: int = 5000,
    ):
        if self._conn is None:
            return
        result = self._conn.execute(
            """
            SELECT ts, yes_price, volume
            FROM normalized_trades
            WHERE market_id = ?
              AND platform = ?
              AND ts BETWEEN ? AND ?
            ORDER BY ts ASC
            """,
            [market_id, platform, int(start_ts), int(end_ts)],
        )
        while True:
            batch = result.fetchmany(batch_size)
            if not batch:
                break
            for ts, yes_price, volume in batch:
                yield PricePoint(
                    timestamp=int(ts),
                    yes_price=float(yes_price),
                    no_price=round(1.0 - float(yes_price), 6),
                    volume=float(volume or 0.0),
                )

    def stream_market_history_batch(
        self,
        market_ids: list[str],
        platform: str,
        start_ts: int,
        end_ts: int,
        batch_size: int = 5000,
    ):
        if self._conn is None or not market_ids:
            return
        placeholders = ", ".join("?" for _ in market_ids)
        params = [*market_ids, platform, int(start_ts), int(end_ts)]
        result = self._conn.execute(
            f"""
            SELECT market_id, ts, yes_price, volume
            FROM normalized_trades
            WHERE market_id IN ({placeholders})
              AND platform = ?
              AND ts BETWEEN ? AND ?
            ORDER BY ts ASC, market_id ASC
            """,
            params,
        )
        while True:
            batch = result.fetchmany(batch_size)
            if not batch:
                break
            for market_id, ts, yes_price, volume in batch:
                yield str(market_id), PricePoint(
                    timestamp=int(ts),
                    yes_price=float(yes_price),
                    no_price=round(1.0 - float(yes_price), 6),
                    volume=float(volume or 0.0),
                )

    def stream_market_history_resampled(
        self,
        market_id: str,
        platform: str,
        start_ts: int,
        end_ts: int,
        bar_seconds: int,
        batch_size: int = 2000,
    ):
        if self._conn is None:
            return
        result = self._conn.execute(
            """
            SELECT
                CAST(FLOOR(ts / ?) AS BIGINT) * ? AS bar_ts,
                SUM(yes_price * volume) / NULLIF(SUM(volume), 0) AS vwap,
                SUM(volume) AS total_volume
            FROM normalized_trades
            WHERE market_id = ?
              AND platform = ?
              AND ts BETWEEN ? AND ?
            GROUP BY bar_ts
            ORDER BY bar_ts ASC
            """,
            [int(bar_seconds), int(bar_seconds), market_id, platform, int(start_ts), int(end_ts)],
        )
        while True:
            batch = result.fetchmany(batch_size)
            if not batch:
                break
            for bar_ts, vwap, total_volume in batch:
                if vwap is None:
                    continue
                yield PricePoint(
                    timestamp=int(bar_ts),
                    yes_price=float(vwap),
                    no_price=round(1.0 - float(vwap), 6),
                    volume=float(total_volume or 0.0),
                )

    def stream_market_history_resampled_batch(
        self,
        market_ids: list[str],
        platform: str,
        start_ts: int,
        end_ts: int,
        bar_seconds: int,
        batch_size: int = 2000,
    ):
        if self._conn is None or not market_ids:
            return
        placeholders = ", ".join("?" for _ in market_ids)
        params = [
            int(bar_seconds),
            int(bar_seconds),
            *market_ids,
            platform,
            int(start_ts),
            int(end_ts),
        ]
        result = self._conn.execute(
            f"""
            SELECT
                market_id,
                CAST(FLOOR(ts / ?) AS BIGINT) * ? AS bar_ts,
                SUM(yes_price * volume) / NULLIF(SUM(volume), 0) AS vwap,
                SUM(volume) AS total_volume
            FROM normalized_trades
            WHERE market_id IN ({placeholders})
              AND platform = ?
              AND ts BETWEEN ? AND ?
            GROUP BY market_id, bar_ts
            ORDER BY bar_ts ASC, market_id ASC
            """,
            params,
        )
        while True:
            batch = result.fetchmany(batch_size)
            if not batch:
                break
            for market_id, bar_ts, vwap, total_volume in batch:
                if vwap is None:
                    continue
                yield str(market_id), PricePoint(
                    timestamp=int(bar_ts),
                    yes_price=float(vwap),
                    no_price=round(1.0 - float(vwap), 6),
                    volume=float(total_volume or 0.0),
                )

    def get_latest_price_before(self, market_id: str, platform: str, ts: int) -> float | None:
        if self._conn is None:
            return None
        row = self._conn.execute(
            """
            SELECT yes_price
            FROM normalized_trades
            WHERE market_id = ?
              AND platform = ?
              AND ts <= ?
            ORDER BY ts DESC
            LIMIT 1
            """,
            [market_id, platform, int(ts)],
        ).fetchone()
        return float(row[0]) if row else None

    def get_latest_prices_before_batch(self, market_ids: list[str], platform: str, ts: int) -> dict[str, float]:
        if self._conn is None or not market_ids:
            return {}
        placeholders = ", ".join("?" for _ in market_ids)
        rows = self._conn.execute(
            f"""
            SELECT market_id, yes_price
            FROM (
                SELECT
                    market_id,
                    yes_price,
                    ROW_NUMBER() OVER (PARTITION BY market_id ORDER BY ts DESC) AS rn
                FROM normalized_trades
                WHERE market_id IN ({placeholders})
                  AND platform = ?
                  AND ts <= ?
            ) ranked
            WHERE rn = 1
            """,
            [*market_ids, platform, int(ts)],
        ).fetchall()
        return {str(market_id): float(yes_price) for market_id, yes_price in rows}

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
