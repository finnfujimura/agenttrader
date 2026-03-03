"""drop legacy price_history constraint

Revision ID: 0004_drop_legacy_price_history_constraint
Revises: 0003_backtest_execution_mode
Create Date: 2026-03-03 00:00:00
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0004_drop_legacy_price_history_constraint"
down_revision: Union[str, None] = "0003_backtest_execution_mode"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("price_history") as batch:
        batch.drop_constraint("uq_price_market_ts", type_="unique")


def downgrade() -> None:
    with op.batch_alter_table("price_history") as batch:
        batch.create_unique_constraint("uq_price_market_ts", ["market_id", "timestamp"])
