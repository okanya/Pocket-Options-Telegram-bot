"""add trade mode

Revision ID: 202605060001
Revises: 202605040001
Create Date: 2026-05-06
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "202605060001"
down_revision = "202605040001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_signals_mode", "signals", type_="check")
    op.create_check_constraint("ck_signals_mode", "signals", "mode in ('signal_only', 'paper', 'demo', 'real')")
    op.add_column("trades", sa.Column("mode", sa.String(length=16), nullable=False, server_default="paper"))
    op.create_check_constraint("ck_trades_mode", "trades", "mode in ('paper', 'demo', 'real')")
    op.create_index("ix_trades_mode_opened_at", "trades", ["mode", "opened_at"])


def downgrade() -> None:
    op.drop_index("ix_trades_mode_opened_at", table_name="trades")
    op.drop_constraint("ck_trades_mode", "trades", type_="check")
    op.drop_column("trades", "mode")
    op.drop_constraint("ck_signals_mode", "signals", type_="check")
    op.create_check_constraint("ck_signals_mode", "signals", "mode in ('signal_only', 'demo', 'real')")
