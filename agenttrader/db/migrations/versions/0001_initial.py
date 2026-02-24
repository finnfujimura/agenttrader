"""initial schema

Revision ID: 0001_initial
Revises: 
Create Date: 2026-02-24 00:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "markets",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("condition_id", sa.Text(), nullable=True),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("tags", sa.Text(), nullable=True),
        sa.Column("market_type", sa.Text(), nullable=False),
        sa.Column("scalar_low", sa.Float(), nullable=True),
        sa.Column("scalar_high", sa.Float(), nullable=True),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("close_time", sa.Integer(), nullable=True),
        sa.Column("resolved", sa.Integer(), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column("last_synced", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "price_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("market_id", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("timestamp", sa.Integer(), nullable=False),
        sa.Column("yes_price", sa.Float(), nullable=False),
        sa.Column("no_price", sa.Float(), nullable=True),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("market_id", "timestamp", name="uq_price_market_ts"),
    )
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("strategy_path", sa.Text(), nullable=False),
        sa.Column("strategy_hash", sa.Text(), nullable=False),
        sa.Column("start_date", sa.Text(), nullable=False),
        sa.Column("end_date", sa.Text(), nullable=False),
        sa.Column("initial_cash", sa.Float(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("results_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "paper_portfolios",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("strategy_path", sa.Text(), nullable=False),
        sa.Column("strategy_hash", sa.Text(), nullable=False),
        sa.Column("initial_cash", sa.Float(), nullable=False),
        sa.Column("cash_balance", sa.Float(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.Integer(), nullable=False),
        sa.Column("stopped_at", sa.Integer(), nullable=True),
        sa.Column("last_reload", sa.Integer(), nullable=True),
        sa.Column("reload_count", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "positions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("market_id", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("contracts", sa.Float(), nullable=False),
        sa.Column("avg_cost", sa.Float(), nullable=False),
        sa.Column("opened_at", sa.Integer(), nullable=False),
        sa.Column("closed_at", sa.Integer(), nullable=True),
        sa.Column("realized_pnl", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "trades",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("market_id", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("contracts", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("slippage", sa.Float(), nullable=False),
        sa.Column("filled_at", sa.Integer(), nullable=False),
        sa.Column("pnl", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "strategy_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("portfolio_id", sa.Text(), nullable=False),
        sa.Column("timestamp", sa.Integer(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("strategy_logs")
    op.drop_table("trades")
    op.drop_table("positions")
    op.drop_table("paper_portfolios")
    op.drop_table("backtest_runs")
    op.drop_table("price_history")
    op.drop_table("markets")
