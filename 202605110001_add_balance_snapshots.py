"""add balance snapshots

Revision ID: 202605110001
Revises: 202605060001
Create Date: 2026-05-11
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "202605110001"
down_revision = "202605060001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "balance_snapshots",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("account_type", sa.String(length=16), nullable=False),
        sa.Column("balance", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_balance_snapshots_account_created_at",
        "balance_snapshots",
        ["account_type", "created_at"],
    )
    op.create_index("ix_balance_snapshots_created_at", "balance_snapshots", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_balance_snapshots_created_at", table_name="balance_snapshots")
    op.drop_index("ix_balance_snapshots_account_created_at", table_name="balance_snapshots")
    op.drop_table("balance_snapshots")
