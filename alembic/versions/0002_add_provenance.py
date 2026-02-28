"""add provenance to price_history

Revision ID: 0002_add_provenance
Revises: 0001_initial
Create Date: 2026-02-27 00:00:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_add_provenance"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("price_history", sa.Column("source", sa.Text(), nullable=True, server_default="pmxt"))
    op.add_column("price_history", sa.Column("granularity", sa.Text(), nullable=True, server_default="1h"))
    with op.batch_alter_table("price_history") as batch:
        batch.create_unique_constraint("uq_price_market_platform_ts", ["market_id", "platform", "timestamp"])


def downgrade() -> None:
    with op.batch_alter_table("price_history") as batch:
        batch.drop_constraint("uq_price_market_platform_ts")
    op.drop_column("price_history", "granularity")
    op.drop_column("price_history", "source")
