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

from agenttrader.data.models import Market, MarketType, OrderBook, OrderLevel, Platform, PricePoint


LOGGER = logging.getLogger(__name__)
DATA_DIR = Path.home() / ".agenttrader" / "data"


_POLY_SLUG_CATEGORY = {
    "will": "politics",
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


class ParquetDataAdapter:
    """
    Reads the Jon Becker prediction market parquet dataset via DuckDB.
    Translates parquet records into agenttrader internal models.

    Connection is in-memory. Parquet files are the source of truth.
    """

    def __init__(self, data_dir: Path | None = None):
        self._data_dir = data_dir or DATA_DIR
        self._conn = duckdb.connect() if duckdb is not None else None
        self._polymarket_trades = str(self._data_dir / "polymarket" / "trades" / "*.parquet")
        self._polymarket_markets = str(self._data_dir / "polymarket" / "markets" / "*.parquet")
        self._polymarket_blocks = str(self._data_dir / "polymarket" / "blocks" / "*.parquet")
        self._kalshi_trades = str(self._data_dir / "kalshi" / "trades" / "*.parquet")
        self._kalshi_markets = str(self._data_dir / "kalshi" / "markets" / "*.parquet")

    def is_available(self) -> bool:
        if self._conn is None:
            return False
        try:
            expected = [
                self._data_dir / "polymarket" / "markets",
                self._data_dir / "polymarket" / "trades",
                self._data_dir / "polymarket" / "blocks",
                self._data_dir / "kalshi" / "markets",
                self._data_dir / "kalshi" / "trades",
            ]
            return all(path.exists() and any(path.glob("*.parquet")) for path in expected)
        except Exception:
            return False

    def get_markets(
        self,
        platform: str = "all",
        category: str | None = None,
        resolved_only: bool = False,
        min_volume: float | None = None,
        limit: int = 100,
    ) -> list[Market]:
        self._require_conn()
        markets: list[Market] = []
        wanted = platform.lower()
        if wanted in {"all", "polymarket"}:
            markets.extend(self._get_polymarket_markets(resolved_only=resolved_only, min_volume=min_volume, limit=limit))
        if wanted in {"all", "kalshi"}:
            markets.extend(self._get_kalshi_markets(resolved_only=resolved_only, min_volume=min_volume, limit=limit))

        if category:
            category_l = category.lower()
            markets = [m for m in markets if (m.category or "").lower() == category_l]

        markets.sort(key=lambda m: float(m.volume or 0.0), reverse=True)
        return markets[:limit]

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

    def get_orderbook_snapshot(
        self,
        market_id: str,
        platform: Platform,
        at_ts: int,
        lookback_seconds: int = 300,
    ) -> OrderBook:
        self._require_conn()
        points = self.get_price_history(market_id, platform, at_ts - lookback_seconds, at_ts)
        if not points:
            LOGGER.warning("No trades in lookback window for %s at %s, using neutral orderbook.", market_id, at_ts)
            return OrderBook(
                market_id=market_id,
                timestamp=at_ts,
                bids=[OrderLevel(price=0.49, size=1.0)],
                asks=[OrderLevel(price=0.51, size=1.0)],
            )

        total_volume = sum(max(float(p.volume or 0.0), 0.0) for p in points)
        if total_volume > 0:
            vwap = sum(float(p.yes_price) * max(float(p.volume or 0.0), 0.0) for p in points) / total_volume
        else:
            vwap = sum(float(p.yes_price) for p in points) / max(len(points), 1)
            total_volume = float(len(points))
        return self._synthesize_orderbook(vwap, total_volume, market_id, at_ts)

    def _get_polymarket_markets(self, resolved_only: bool, min_volume: float | None, limit: int) -> list[Market]:
        self._require_conn()
        where = ["1=1"]
        params: list[object] = [self._polymarket_markets]
        if resolved_only:
            where.append("closed = TRUE")
        if min_volume is not None:
            where.append("volume >= ?")
            params.append(float(min_volume))
        params.append(int(limit))
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
            FROM read_parquet(?)
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
            out.append(
                Market(
                    id=str(market_id),
                    condition_id=str(condition_id or market_id),
                    platform=Platform.POLYMARKET,
                    title=str(title or ""),
                    category=self._infer_polymarket_category(str(slug or ""), str(title or "")),
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

    def _get_kalshi_markets(self, resolved_only: bool, min_volume: float | None, limit: int) -> list[Market]:
        self._require_conn()
        where = ["1=1"]
        params: list[object] = [self._kalshi_markets]
        if resolved_only:
            where.append("status = 'finalized'")
        if min_volume is not None:
            where.append("volume / 100.0 >= ?")
            params.append(float(min_volume))
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
            FROM read_parquet(?)
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
            out.append(
                Market(
                    id=str(ticker),
                    condition_id=str(event_ticker or ticker),
                    platform=Platform.KALSHI,
                    title=str(title or ticker),
                    category=self._infer_kalshi_category(str(event_ticker or "")),
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

    def _get_polymarket_price_history(self, market_id: str, start_ts: int, end_ts: int) -> list[PricePoint]:
        self._require_conn()
        yes_token_id = self._get_polymarket_yes_token(market_id)
        if not yes_token_id:
            return []
        query = """
            WITH block_times AS (
                SELECT block_number, CAST(timestamp AS BIGINT) AS ts
                FROM read_parquet(?)
            )
            SELECT
                bt.ts AS timestamp,
                CASE
                    WHEN t.taker_asset_id = ?
                    THEN CAST(t.taker_amount AS DOUBLE) / NULLIF(CAST(t.maker_amount + t.taker_amount AS DOUBLE), 0)
                    ELSE CAST(t.maker_amount AS DOUBLE) / NULLIF(CAST(t.maker_amount + t.taker_amount AS DOUBLE), 0)
                END AS yes_price,
                CAST(t.taker_amount + t.maker_amount AS DOUBLE) / 1000000.0 AS volume
            FROM read_parquet(?) t
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
                self._polymarket_blocks,
                yes_token_id,
                self._polymarket_trades,
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
        query = """
            SELECT
                CAST(EPOCH(created_time) AS BIGINT) AS timestamp,
                yes_price / 100.0 AS yes_price,
                no_price / 100.0 AS no_price,
                count AS volume
            FROM read_parquet(?)
            WHERE ticker = ?
            AND EPOCH(created_time) >= ?
            AND EPOCH(created_time) <= ?
            ORDER BY timestamp ASC
        """
        rows = self._conn.execute(
            query,
            [self._kalshi_trades, market_id, int(start_ts), int(end_ts)],
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
        query = """
            SELECT json_extract_string(clob_token_ids, '$[0]') AS yes_token_id
            FROM read_parquet(?)
            WHERE json_extract_string(clob_token_ids, '$[0]') = ?
               OR condition_id = ?
            LIMIT 1
        """
        row = self._conn.execute(query, [self._polymarket_markets, market_id, market_id]).fetchone()
        return str(row[0]) if row and row[0] else None

    def _synthesize_orderbook(self, vwap: float, total_volume: float, market_id: str, at_ts: int) -> OrderBook:
        spread = max(0.005, min(0.03, 500.0 / (total_volume + 1.0)))
        half = spread / 2.0
        base_size = max(total_volume, 1.0)
        bids = [
            OrderLevel(price=self._clamp_price(vwap - half), size=base_size * 0.4),
            OrderLevel(price=self._clamp_price(vwap - half * 2), size=base_size * 0.3),
            OrderLevel(price=self._clamp_price(vwap - half * 3), size=base_size * 0.2),
        ]
        asks = [
            OrderLevel(price=self._clamp_price(vwap + half), size=base_size * 0.4),
            OrderLevel(price=self._clamp_price(vwap + half * 2), size=base_size * 0.3),
            OrderLevel(price=self._clamp_price(vwap + half * 3), size=base_size * 0.2),
        ]
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)
        return OrderBook(market_id=market_id, timestamp=at_ts, bids=bids, asks=asks)

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
