# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from sqlalchemy import Column, Float, Integer, Text, UniqueConstraint
from sqlalchemy.orm import declarative_base


Base = declarative_base()


class Market(Base):
    __tablename__ = "markets"

    id = Column(Text, primary_key=True)
    condition_id = Column(Text)
    platform = Column(Text, nullable=False)
    title = Column(Text, nullable=False)
    category = Column(Text)
    tags = Column(Text)
    market_type = Column(Text, nullable=False)
    scalar_low = Column(Float)
    scalar_high = Column(Float)
    volume = Column(Float)
    close_time = Column(Integer)
    resolved = Column(Integer, default=0)
    resolution = Column(Text)
    last_synced = Column(Integer)


class PriceHistory(Base):
    __tablename__ = "price_history"
    __table_args__ = (
        UniqueConstraint("market_id", "timestamp", name="uq_price_market_ts"),
        UniqueConstraint("market_id", "platform", "timestamp", name="uq_price_market_platform_ts"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(Text, nullable=False)
    platform = Column(Text, nullable=False)
    timestamp = Column(Integer, nullable=False)
    yes_price = Column(Float, nullable=False)
    no_price = Column(Float)
    volume = Column(Float)
    source = Column(Text, default="pmxt")
    granularity = Column(Text, default="1h")


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id = Column(Text, primary_key=True)
    strategy_path = Column(Text, nullable=False)
    strategy_hash = Column(Text, nullable=False)
    start_date = Column(Text, nullable=False)
    end_date = Column(Text, nullable=False)
    initial_cash = Column(Float, nullable=False)
    status = Column(Text, nullable=False)
    error = Column(Text)
    results_json = Column(Text)
    created_at = Column(Integer, nullable=False)
    completed_at = Column(Integer)


class PaperPortfolio(Base):
    __tablename__ = "paper_portfolios"

    id = Column(Text, primary_key=True)
    strategy_path = Column(Text, nullable=False)
    strategy_hash = Column(Text, nullable=False)
    initial_cash = Column(Float, nullable=False)
    cash_balance = Column(Float, nullable=False)
    status = Column(Text, nullable=False)
    pid = Column(Integer)
    started_at = Column(Integer, nullable=False)
    stopped_at = Column(Integer)
    last_reload = Column(Integer)
    reload_count = Column(Integer, default=0)


class Position(Base):
    __tablename__ = "positions"

    id = Column(Text, primary_key=True)
    portfolio_id = Column(Text, nullable=False)
    market_id = Column(Text, nullable=False)
    platform = Column(Text, nullable=False)
    side = Column(Text, nullable=False)
    contracts = Column(Float, nullable=False)
    avg_cost = Column(Float, nullable=False)
    opened_at = Column(Integer, nullable=False)
    closed_at = Column(Integer)
    realized_pnl = Column(Float)


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Text, primary_key=True)
    portfolio_id = Column(Text, nullable=False)
    market_id = Column(Text, nullable=False)
    platform = Column(Text, nullable=False)
    action = Column(Text, nullable=False)
    side = Column(Text, nullable=False)
    contracts = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    slippage = Column(Float, nullable=False, default=0)
    filled_at = Column(Integer, nullable=False)
    pnl = Column(Float)


class StrategyLog(Base):
    __tablename__ = "strategy_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    portfolio_id = Column(Text, nullable=False)
    timestamp = Column(Integer, nullable=False)
    message = Column(Text, nullable=False)
