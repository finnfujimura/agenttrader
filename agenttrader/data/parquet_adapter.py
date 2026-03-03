# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

try:  # pragma: no cover - import availability depends on runtime environment
    import duckdb
except ImportError:  # pragma: no cover
    duckdb = None

from agenttrader.data.models import DataProvenance, Market, MarketType, Platform, PricePoint


LOGGER = logging.getLogger(__name__)
DATA_DIR = Path.home() / ".agenttrader" / "data"


_POLY_SLUG_CATEGORY = {
    "election": "politics",
    "politics": "politics",
    "presidential": "politics",
    "bitcoin": "crypto",
    "btc": "crypto",
    "ethereum": "crypto",
    "eth": "crypto",
    "solana": "crypto",
    "crypto": "crypto",
    "sports": "sports",
    "nba": "sports",
    "nfl": "sports",
    "mlb": "sports",
    "soccer": "sports",
}

# Reverse map: category -> list of keywords that map to it
_POLY_CATEGORY_KEYWORDS: dict[str, list[str]] = {}
for _kw, _cat in _POLY_SLUG_CATEGORY.items():
    _POLY_CATEGORY_KEYWORDS.setdefault(_cat, []).append(_kw)


def _safe_parquet_glob(directory: str) -> list[str]:
    """Return sorted parquet files excluding AppleDouble metadata files."""
    path = Path(directory)
    if not path.exists():
        return []
    files = [str(f) for f in path.glob("*.parquet") if not f.name.startswith("._")]
    return sorted(files)


class ParquetDataAdapter:
    """
    Reads the Jon Becker prediction market parquet dataset via DuckDB.
    Translates parquet records into agenttrader internal models.

    Connection is in-memory. Parquet files are the source of truth.
    """

    def __init__(self, data_dir: Path | None = None):
        self._data_dir = data_dir or DATA_DIR
        self._conn = duckdb.connect() if duckdb is not None else None
        self._poly_trades_files = _safe_parquet_glob(str(self._data_dir / "polymarket" / "trades"))
        self._poly_markets_files = _safe_parquet_glob(str(self._data_dir / "polymarket" / "markets"))
        self._poly_blocks_files = _safe_parquet_glob(str(self._data_dir / "polymarket" / "blocks"))
        self._kalshi_trades_files = _safe_parquet_glob(str(self._data_dir / "kalshi" / "trades"))
        self._kalshi_markets_files = _safe_parquet_glob(str(self._data_dir / "kalshi" / "markets"))

        self._poly_trades_view: str | None = None
        self._poly_markets_view: str | None = None
        self._poly_blocks_view: str | None = None
        self._kalshi_trades_view: str | None = None
        self._kalshi_markets_view: str | None = None

        if self._conn is not None:
            try:
                self._poly_trades_view = self._create_view("poly_trades", self._poly_trades_files)
                self._poly_markets_view = self._create_view("poly_markets", self._poly_markets_files)
                self._poly_blocks_view = self._create_view("poly_blocks", self._poly_blocks_files)
                self._kalshi_trades_view = self._create_view("kalshi_trades", self._kalshi_trades_files)
                self._kalshi_markets_view = self._create_view("kalshi_markets", self._kalshi_markets_files)
            except Exception:
                self._conn.close()
                self._conn = None
                raise

    def is_available(self) -> bool:
        if self._conn is None:
            return False
        return len(self._poly_markets_files) > 0 or len(self._kalshi_markets_files) > 0

    def get_markets(
        self,
        platform: str = "all",
        category: str | None = None,
        active_only: bool = False,
        resolved_only: bool = False,
        min_volume: float | None = None,
        limit: int = 100,
    ) -> list[Market]:
        self._require_conn()
        wanted = platform.lower()
        if wanted == "polymarket":
            return self._get_polymarket_markets(category, active_only, resolved_only, min_volume, limit)
        if wanted == "kalshi":
            return self._get_kalshi_markets(category, active_only, resolved_only, min_volume, limit)
        if wanted == "all":
            per_platform = max(limit // 2, 1)
            poly = self._get_polymarket_markets(category, active_only, resolved_only, min_volume, per_platform)
            kalshi = self._get_kalshi_markets(category, active_only, resolved_only, min_volume, per_platform)
            combined = poly + kalshi
            combined.sort(key=lambda m: float(m.volume or 0.0), reverse=True)
            return combined[:limit]
        raise ValueError(f"Unknown platform: {platform}. Use 'polymarket', 'kalshi', or 'all'.")

    def get_markets_by_ids(
        self,
        market_ids: list[str],
        platform: str = "all",
    ) -> list[Market]:
        """Look up specific markets by ID. Queries parquet directly — no volume/limit filtering."""
        self._require_conn()
        if not market_ids:
            return []
        wanted = platform.lower()
        results: list[Market] = []
        if wanted in ("polymarket", "all") and self._poly_markets_view:
            results.extend(self._get_polymarket_markets_by_ids(market_ids))
        if wanted in ("kalshi", "all") and self._kalshi_markets_view:
            results.extend(self._get_kalshi_markets_by_ids(market_ids))
        return results

    def _get_polymarket_markets_by_ids(self, market_ids: list[str]) -> list[Market]:
        """Look up polymarket markets where the yes-token ID or condition_id matches."""
        placeholders = ", ".join("?" for _ in market_ids)
        query = f"""
            SELECT
                json_extract_string(clob_token_ids, '$[0]') AS id,
                condition_id,
                question AS title,
                slug,
                volume,
                closed,
                end_date,
                json_extract_string(outcome_prices, '$[0]') AS yes_price_str
            FROM {self._poly_markets_view}
            WHERE json_extract_string(clob_token_ids, '$[0]') IN ({placeholders})
               OR condition_id IN ({placeholders})
        """
        params = list(market_ids) + list(market_ids)
        rows = self._conn.execute(query, params).fetchall()
        out: list[Market] = []
        for row in rows:
            market_id, condition_id, title, slug, volume, closed, end_date, yes_price_str = row
            if not market_id:
                continue
            yes_price = self._to_float(yes_price_str)
            resolution = None
            if bool(closed) and yes_price is not None:
                if yes_price >= 0.999:
                    resolution = "yes"
                elif yes_price <= 0.001:
                    resolution = "no"
            inferred_category = self._infer_polymarket_category(str(slug or ""), str(title or ""))
            out.append(
                Market(
                    id=str(market_id),
                    condition_id=str(condition_id or market_id),
                    platform=Platform.POLYMARKET,
                    title=str(title or ""),
                    category=inferred_category,
                    tags=[],
                    market_type=MarketType.BINARY,
                    volume=float(volume or 0.0),
                    close_time=self._to_unix_ts(end_date),
                    resolved=bool(closed),
                    resolution=resolution,
                    scalar_low=None,
                    scalar_high=None,
                )
            )
        return out

    def _get_kalshi_markets_by_ids(self, market_ids: list[str]) -> list[Market]:
        """Look up kalshi markets where the ticker matches."""
        placeholders = ", ".join("?" for _ in market_ids)
        query = f"""
            SELECT
                ticker,
                event_ticker,
                title,
                market_type,
                status,
                volume / 100.0 AS volume_dollars,
                close_time,
                result
            FROM {self._kalshi_markets_view}
            WHERE ticker IN ({placeholders})
        """
        rows = self._conn.execute(query, list(market_ids)).fetchall()
        out: list[Market] = []
        for row in rows:
            ticker, event_ticker, title, market_type, status, volume, close_time, result = row
            if not ticker:
                continue
            norm_result = str(result or "").strip().lower() or None
            inferred_category = self._infer_kalshi_category(str(event_ticker or ""))
            out.append(
                Market(
                    id=str(ticker),
                    condition_id=str(event_ticker or ticker),
                    platform=Platform.KALSHI,
                    title=str(title or ticker),
                    category=inferred_category,
                    tags=[],
                    market_type=MarketType.BINARY if str(market_type or "").lower() == "binary" else MarketType.SCALAR,
                    volume=float(volume or 0.0),
                    close_time=self._to_unix_ts(close_time),
                    resolved=str(status or "").lower() == "finalized",
                    resolution=norm_result,
                    scalar_low=None,
                    scalar_high=None,
                )
            )
        return out

    def get_price_history(
        self,
        market_id: str,
        platform: Platform,
        start_ts: int,
        end_ts: int,
    ) -> list[PricePoint]:
        self._require_conn()
        if platform == Platform.POLYMARKET:
            return self._get_polymarket_price_history(market_id, start_ts, end_ts)
        return self._get_kalshi_price_history(market_id, start_ts, end_ts)

    def get_provenance(self, market_id, platform) -> DataProvenance:
        _ = (market_id, platform)
        return DataProvenance(source="parquet", observed=True, granularity="trade")

    def _get_polymarket_markets(
        self,
        category: str | None,
        active_only: bool = False,
        resolved_only: bool = False,
        min_volume: float | None = None,
        limit: int = 100,
    ) -> list[Market]:
        self._require_conn()
        if not self._poly_markets_view:
            return []
        where = ["1=1"]
        params: list[object] = []
        if active_only:
            where.append("closed = FALSE")
        if resolved_only:
            where.append("closed = TRUE")
        if min_volume is not None:
            where.append("volume >= ?")
            params.append(float(min_volume))
        fetch_limit = int(limit)
        # When category is active, add SQL pre-filter using ILIKE on slug/question
        # to narrow rows before Python-level exact category inference.
        # The Python filter remains authoritative; this just avoids a full table scan.
        if category:
            keywords = _POLY_CATEGORY_KEYWORDS.get(category.lower(), [])
            if not keywords:
                return []
            ilike_clauses = []
            for kw in keywords:
                ilike_clauses.append("slug ILIKE ?")
                params.append(f"%{kw}%")
                ilike_clauses.append("question ILIKE ?")
                params.append(f"%{kw}%")
            where.append(f"({' OR '.join(ilike_clauses)})")
            fetch_limit = min(max(int(limit) * 20, int(limit)), 5000)
        params.append(fetch_limit)
        query = f"""
            SELECT
                json_extract_string(clob_token_ids, '$[0]') AS id,
                condition_id,
                question AS title,
                slug,
                volume,
                closed,
                end_date,
                json_extract_string(outcome_prices, '$[0]') AS yes_price_str
            FROM {self._poly_markets_view}
            WHERE {' AND '.join(where)}
            ORDER BY volume DESC
            LIMIT ?
        """
        rows = self._conn.execute(query, params).fetchall()
        out: list[Market] = []
        for row in rows:
            market_id, condition_id, title, slug, volume, closed, end_date, yes_price_str = row
            if not market_id:
                continue
            yes_price = self._to_float(yes_price_str)
            resolution = None
            if bool(closed) and yes_price is not None:
                if yes_price >= 0.999:
                    resolution = "yes"
                elif yes_price <= 0.001:
                    resolution = "no"
            inferred_category = self._infer_polymarket_category(str(slug or ""), str(title or ""))
            if category and inferred_category.lower() != category.lower():
                continue
            out.append(
                Market(
                    id=str(market_id),
                    condition_id=str(condition_id or market_id),
                    platform=Platform.POLYMARKET,
                    title=str(title or ""),
                    category=inferred_category,
                    tags=[],
                    market_type=MarketType.BINARY,
                    volume=float(volume or 0.0),
                    close_time=self._to_unix_ts(end_date),
                    resolved=bool(closed),
                    resolution=resolution,
                    scalar_low=None,
                    scalar_high=None,
                )
            )
        return out[:limit]

    def _get_kalshi_markets(
        self,
        category: str | None,
        active_only: bool = False,
        resolved_only: bool = False,
        min_volume: float | None = None,
        limit: int = 100,
    ) -> list[Market]:
        self._require_conn()
        if not self._kalshi_markets_view:
            return []
        where = ["1=1"]
        params: list[object] = []
        if active_only:
            where.append("LOWER(status) != 'finalized'")
        if resolved_only:
            where.append("status = 'finalized'")
        if min_volume is not None:
            where.append("volume / 100.0 >= ?")
            params.append(float(min_volume))
        # Kalshi category = uppercase prefix of event_ticker (e.g. KXFEDDECISION -> kxfeddecision).
        # This is exact, so we can push it into SQL and keep LIMIT.
        if category:
            where.append("LOWER(REGEXP_EXTRACT(event_ticker, '^([A-Z]+)', 1)) = ?")
            params.append(category.lower())
        params.append(int(limit))
        query = f"""
            SELECT
                ticker,
                event_ticker,
                title,
                market_type,
                status,
                volume / 100.0 AS volume_dollars,
                close_time,
                result
            FROM {self._kalshi_markets_view}
            WHERE {' AND '.join(where)}
            ORDER BY volume DESC
            LIMIT ?
        """
        rows = self._conn.execute(query, params).fetchall()
        out: list[Market] = []
        for row in rows:
            ticker, event_ticker, title, market_type, status, volume, close_time, result = row
            if not ticker:
                continue
            norm_result = str(result or "").strip().lower() or None
            inferred_category = self._infer_kalshi_category(str(event_ticker or ""))
            if category and inferred_category.lower() != category.lower():
                continue
            out.append(
                Market(
                    id=str(ticker),
                    condition_id=str(event_ticker or ticker),
                    platform=Platform.KALSHI,
                    title=str(title or ticker),
                    category=inferred_category,
                    tags=[],
                    market_type=MarketType.BINARY if str(market_type or "").lower() == "binary" else MarketType.SCALAR,
                    volume=float(volume or 0.0),
                    close_time=self._to_unix_ts(close_time),
                    resolved=str(status or "").lower() == "finalized",
                    resolution=norm_result,
                    scalar_low=None,
                    scalar_high=None,
                )
            )
        return out[:limit]

    def _get_polymarket_price_history(self, market_id: str, start_ts: int, end_ts: int) -> list[PricePoint]:
        self._require_conn()
        if not self._poly_trades_view or not self._poly_blocks_view:
            return []
        yes_token_id = self._get_polymarket_yes_token(market_id)
        if not yes_token_id:
            return []
        query = f"""
            WITH block_times AS (
                SELECT block_number, ts
                FROM (
                    SELECT
                        block_number,
                        CASE
                            WHEN TRY_CAST(timestamp AS BIGINT) IS NOT NULL
                                THEN TRY_CAST(timestamp AS BIGINT)
                            WHEN TRY_CAST(timestamp AS TIMESTAMP) IS NOT NULL
                                THEN CAST(EPOCH(TRY_CAST(timestamp AS TIMESTAMP)) AS BIGINT)
                            ELSE NULL
                        END AS ts
                    FROM {self._poly_blocks_view}
                )
                WHERE ts IS NOT NULL
            )
            SELECT
                bt.ts AS timestamp,
                CASE
                    WHEN t.taker_asset_id = ?
                    THEN CAST(t.taker_amount AS DOUBLE) / NULLIF(CAST(t.maker_amount + t.taker_amount AS DOUBLE), 0)
                    ELSE CAST(t.maker_amount AS DOUBLE) / NULLIF(CAST(t.maker_amount + t.taker_amount AS DOUBLE), 0)
                END AS yes_price,
                CAST(t.taker_amount + t.maker_amount AS DOUBLE) / 1000000.0 AS volume
            FROM {self._poly_trades_view} t
            JOIN block_times bt ON t.block_number = bt.block_number
            WHERE (
                t.taker_asset_id = ? OR t.maker_asset_id = ?
            )
            AND bt.ts >= ?
            AND bt.ts <= ?
            ORDER BY bt.ts ASC
        """
        rows = self._conn.execute(
            query,
            [
                yes_token_id,
                yes_token_id,
                yes_token_id,
                int(start_ts),
                int(end_ts),
            ],
        ).fetchall()
        out: list[PricePoint] = []
        for row in rows:
            ts, yes_price, volume = row
            if yes_price is None:
                continue
            yes = min(1.0, max(0.0, float(yes_price)))
            out.append(
                PricePoint(
                    timestamp=int(ts),
                    yes_price=yes,
                    no_price=1.0 - yes,
                    volume=float(volume or 0.0),
                )
            )
        return out

    def _get_kalshi_price_history(self, market_id: str, start_ts: int, end_ts: int) -> list[PricePoint]:
        self._require_conn()
        if not self._kalshi_trades_view:
            return []
        query = f"""
            SELECT
                CAST(EPOCH(created_time) AS BIGINT) AS timestamp,
                yes_price / 100.0 AS yes_price,
                no_price / 100.0 AS no_price,
                count AS volume
            FROM {self._kalshi_trades_view}
            WHERE ticker = ?
            AND EPOCH(created_time) >= ?
            AND EPOCH(created_time) <= ?
            ORDER BY timestamp ASC
        """
        rows = self._conn.execute(
            query,
            [market_id, int(start_ts), int(end_ts)],
        ).fetchall()
        return [
            PricePoint(
                timestamp=int(ts),
                yes_price=min(1.0, max(0.0, float(yes_price))),
                no_price=min(1.0, max(0.0, float(no_price))) if no_price is not None else None,
                volume=float(volume or 0.0),
            )
            for ts, yes_price, no_price, volume in rows
            if yes_price is not None
        ]

    def _get_polymarket_yes_token(self, market_id: str) -> str | None:
        self._require_conn()
        if not self._poly_markets_view:
            return None
        query = f"""
            SELECT json_extract_string(clob_token_ids, '$[0]') AS yes_token_id
            FROM {self._poly_markets_view}
            WHERE json_extract_string(clob_token_ids, '$[0]') = ?
               OR condition_id = ?
            LIMIT 1
        """
        row = self._conn.execute(query, [market_id, market_id]).fetchone()
        return str(row[0]) if row and row[0] else None

    def _create_view(self, view_name: str, files: list[str]) -> str | None:
        self._require_conn()
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", view_name):
            raise ValueError(f"Invalid view name: {view_name!r}")
        if not files:
            return None
        quoted_files = ", ".join("'" + str(path).replace("'", "''") + "'" for path in files)
        self._conn.execute(f"CREATE OR REPLACE VIEW {view_name} AS SELECT * FROM read_parquet([{quoted_files}])")
        return view_name

    @staticmethod
    def _clamp_price(value: float) -> float:
        return round(max(0.0, min(1.0, float(value))), 4)

    @staticmethod
    def _to_float(value: object) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _to_unix_ts(value: object) -> int:
        if value is None:
            return 0
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return int(value.timestamp())
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip()
        if not text:
            return 0
        try:
            return int(float(text))
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return int(dt.timestamp())
        except ValueError:
            return 0

    @staticmethod
    def _infer_polymarket_category(slug: str, title: str) -> str:
        source = slug or title
        if not source:
            return "other"
        first = source.lower().split("-")[0]
        if first in _POLY_SLUG_CATEGORY:
            return _POLY_SLUG_CATEGORY[first]
        for key, mapped in _POLY_SLUG_CATEGORY.items():
            if key in source.lower():
                return mapped
        return "other"

    @staticmethod
    def _infer_kalshi_category(event_ticker: str) -> str:
        match = re.match(r"^([A-Z]+)", str(event_ticker or ""))
        if not match:
            return "other"
        return match.group(1).lower()

    def _require_conn(self) -> None:
        if self._conn is None:
            raise RuntimeError("duckdb is not installed. Install with: pip install duckdb")
