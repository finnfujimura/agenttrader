"""add execution_mode to backtest_runs

Revision ID: 0003_backtest_execution_mode
Revises: 0002_add_provenance
Create Date: 2026-02-27 00:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_backtest_execution_mode"
down_revision: Union[str, None] = "0002_add_provenance"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("backtest_runs", sa.Column("execution_mode", sa.Text(), nullable=True, server_default="strict_price_only"))


def downgrade() -> None:
    op.drop_column("backtest_runs", "execution_mode")
