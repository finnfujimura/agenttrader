# DO NOT import dome_api_sdk here. Use agenttrader.data.dome_client only.
from __future__ import annotations

import json

from sqlalchemy import and_, delete, desc, func, select
from sqlalchemy.dialects.sqlite import insert

from agenttrader.data.models import Market, MarketType, Platform, PricePoint
from agenttrader.db import get_session
from agenttrader.db.schema import BacktestRun, Market as MarketRow, PaperPortfolio, Position, PriceHistory, StrategyLog, Trade


class DataCache:
    def __init__(self, engine):
        self._engine = engine

    def upsert_market(self, market: Market) -> None:
        payload = {
            "id": market.id,
            "condition_id": market.condition_id,
            "platform": market.platform.value if isinstance(market.platform, Platform) else str(market.platform),
            "title": market.title,
            "category": market.category,
            "tags": json.dumps(market.tags),
            "market_type": market.market_type.value if isinstance(market.market_type, MarketType) else str(market.market_type),
            "scalar_low": market.scalar_low,
            "scalar_high": market.scalar_high,
            "volume": market.volume,
            "close_time": market.close_time,
            "resolved": 1 if market.resolved else 0,
            "resolution": market.resolution,
            "last_synced": None,
        }
        stmt = insert(MarketRow).values(**payload)
        stmt = stmt.on_conflict_do_update(index_elements=[MarketRow.id], set_=payload)
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def mark_market_synced(self, market_id: str, timestamp: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                MarketRow.__table__.update().where(MarketRow.id == market_id).values(last_synced=timestamp)
            )

    def upsert_price_point(self, market_id: str, platform: str, point: PricePoint) -> None:
        payload = {
            "market_id": market_id,
            "platform": platform,
            "timestamp": point.timestamp,
            "yes_price": point.yes_price,
            "no_price": point.no_price,
            "volume": point.volume,
        }
        stmt = insert(PriceHistory).values(**payload)
        stmt = stmt.on_conflict_do_nothing(index_elements=[PriceHistory.market_id, PriceHistory.timestamp])
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def get_markets(
        self,
        platform: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        min_volume: float | None = None,
        limit: int = 100,
    ) -> list[Market]:
        query = select(MarketRow)
        if platform and platform != "all":
            query = query.where(MarketRow.platform == platform)
        if category:
            query = query.where(MarketRow.category == category)
        if min_volume is not None:
            query = query.where(MarketRow.volume >= min_volume)
        query = query.order_by(desc(MarketRow.volume)).limit(limit)

        with get_session(self._engine) as session:
            rows = list(session.scalars(query).all())

        markets = [self._to_market(r) for r in rows]
        if tags:
            wanted = {t.lower() for t in tags}
            markets = [m for m in markets if wanted.issubset({t.lower() for t in m.tags})]
        return markets

    def search_markets(self, query: str, platform: str = "all", limit: int = 100) -> list[Market]:
        q = select(MarketRow).where(MarketRow.title.ilike(f"%{query}%"))
        if platform != "all":
            q = q.where(MarketRow.platform == platform)
        q = q.limit(limit)
        with get_session(self._engine) as session:
            rows = list(session.scalars(q).all())
        return [self._to_market(r) for r in rows]

    def get_market(self, market_id: str) -> Market | None:
        with get_session(self._engine) as session:
            row = session.get(MarketRow, market_id)
            return self._to_market(row) if row else None

    def get_price_history(
        self,
        market_id: str,
        start_ts: int,
        end_ts: int,
    ) -> list[PricePoint]:
        q = (
            select(PriceHistory)
            .where(
                and_(
                    PriceHistory.market_id == market_id,
                    PriceHistory.timestamp >= start_ts,
                    PriceHistory.timestamp <= end_ts,
                )
            )
            .order_by(PriceHistory.timestamp.asc())
        )
        with get_session(self._engine) as session:
            rows = list(session.scalars(q).all())
        return [self._to_price_point(r) for r in rows]

    def get_latest_price(self, market_id: str) -> PricePoint | None:
        q = (
            select(PriceHistory)
            .where(PriceHistory.market_id == market_id)
            .order_by(PriceHistory.timestamp.desc())
            .limit(1)
        )
        with get_session(self._engine) as session:
            row = session.scalars(q).first()
        if not row:
            return None
        return self._to_price_point(row)

    def list_backtest_runs(self, limit: int = 100) -> list[BacktestRun]:
        q = select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(limit)
        with get_session(self._engine) as session:
            return list(session.scalars(q).all())

    def get_backtest_run(self, run_id: str) -> BacktestRun | None:
        with get_session(self._engine) as session:
            return session.get(BacktestRun, run_id)

    def list_paper_portfolios(self) -> list[PaperPortfolio]:
        q = select(PaperPortfolio).order_by(PaperPortfolio.started_at.desc())
        with get_session(self._engine) as session:
            return list(session.scalars(q).all())

    def get_portfolio(self, portfolio_id: str) -> PaperPortfolio | None:
        with get_session(self._engine) as session:
            return session.get(PaperPortfolio, portfolio_id)

    def get_open_positions(self, portfolio_id: str) -> list[Position]:
        q = select(Position).where(and_(Position.portfolio_id == portfolio_id, Position.closed_at.is_(None)))
        with get_session(self._engine) as session:
            return list(session.scalars(q).all())

    def get_trades(self, portfolio_id: str, limit: int = 200) -> list[Trade]:
        q = (
            select(Trade)
            .where(Trade.portfolio_id == portfolio_id)
            .order_by(Trade.filled_at.desc())
            .limit(limit)
        )
        with get_session(self._engine) as session:
            return list(session.scalars(q).all())

    def append_log(self, portfolio_id: str, ts: int, message: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                insert(StrategyLog).values(portfolio_id=portfolio_id, timestamp=ts, message=message)
            )

    def get_logs(self, portfolio_id: str, limit: int = 100) -> list[dict]:
        q = (
            select(StrategyLog)
            .where(StrategyLog.portfolio_id == portfolio_id)
            .order_by(StrategyLog.timestamp.desc())
            .limit(limit)
        )
        with get_session(self._engine) as session:
            rows = list(session.scalars(q).all())
        return [{"timestamp": r.timestamp, "message": r.message} for r in rows]

    def prune_price_history(self, older_than_ts: int, dry_run: bool = False) -> int:
        with get_session(self._engine) as session:
            count = session.scalar(
                select(func.count()).select_from(PriceHistory).where(PriceHistory.timestamp < older_than_ts)
            )
            if not dry_run and count:
                session.execute(delete(PriceHistory).where(PriceHistory.timestamp < older_than_ts))
                session.commit()
        return int(count or 0)

    @staticmethod
    def _to_market(row: MarketRow) -> Market:
        return Market(
            id=row.id,
            condition_id=row.condition_id or row.id,
            platform=Platform(row.platform),
            title=row.title,
            category=row.category or "",
            tags=json.loads(row.tags) if row.tags else [],
            market_type=MarketType(row.market_type),
            volume=float(row.volume or 0),
            close_time=int(row.close_time or 0),
            resolved=bool(row.resolved),
            resolution=row.resolution,
            scalar_low=row.scalar_low,
            scalar_high=row.scalar_high,
        )

    @staticmethod
    def _to_price_point(row: PriceHistory) -> PricePoint:
        return PricePoint(
            timestamp=int(row.timestamp),
            yes_price=float(row.yes_price),
            no_price=float(row.no_price) if row.no_price is not None else None,
            volume=float(row.volume or 0),
        )
