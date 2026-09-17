"""initial schema

Revision ID: 202605040001
Revises:
Create Date: 2026-05-04
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "202605040001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_users",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("first_name", sa.String(length=255), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="viewer"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("role in ('admin', 'viewer')", name="ck_telegram_users_role"),
        sa.UniqueConstraint("telegram_user_id", name="uq_telegram_users_telegram_user_id"),
    )

    op.create_table(
        "settings",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_by_telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("key", name="uq_settings_key"),
    )

    op.create_table(
        "candles",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("asset", sa.String(length=64), nullable=False),
        sa.Column("timeframe_seconds", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(18, 8), nullable=False),
        sa.Column("high", sa.Numeric(18, 8), nullable=False),
        sa.Column("low", sa.Numeric(18, 8), nullable=False),
        sa.Column("close", sa.Numeric(18, 8), nullable=False),
        sa.Column("volume", sa.Numeric(18, 8), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("asset", "timeframe_seconds", "timestamp", name="uq_candles_asset_tf_ts"),
    )
    op.create_index("ix_candles_asset_tf_ts", "candles", ["asset", "timeframe_seconds", "timestamp"])

    op.create_table(
        "signals",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("asset", sa.String(length=64), nullable=False),
        sa.Column("strategy_name", sa.String(length=128), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("timeframe_seconds", sa.Integer(), nullable=False),
        sa.Column("expiration_seconds", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(18, 8), nullable=True),
        sa.Column("candle_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("reason", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("is_selected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_trade_allowed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("rejection_reason", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("direction in ('buy', 'sell')", name="ck_signals_direction"),
        sa.CheckConstraint("mode in ('signal_only', 'demo', 'real')", name="ck_signals_mode"),
    )
    op.create_index("ix_signals_asset_strategy_created_at", "signals", ["asset", "strategy_name", "created_at"])
    op.create_index("ix_signals_candle_timestamp", "signals", ["candle_timestamp"])

    op.create_table(
        "trades",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("external_trade_id", sa.String(length=255), nullable=True),
        sa.Column("signal_id", sa.BigInteger(), sa.ForeignKey("signals.id", ondelete="SET NULL"), nullable=True),
        sa.Column("asset", sa.String(length=64), nullable=False),
        sa.Column("strategy_name", sa.String(length=128), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("amount", sa.Numeric(18, 8), nullable=False),
        sa.Column("expiration_seconds", sa.Integer(), nullable=False),
        sa.Column("open_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("close_price", sa.Numeric(18, 8), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=False, server_default="unknown"),
        sa.Column("profit", sa.Numeric(18, 8), nullable=True),
        sa.Column("payout", sa.Numeric(8, 4), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("direction in ('buy', 'sell')", name="ck_trades_direction"),
        sa.CheckConstraint("result in ('win', 'loss', 'draw', 'unknown')", name="ck_trades_result"),
        sa.CheckConstraint("status in ('opened', 'closed', 'failed', 'cancelled')", name="ck_trades_status"),
    )
    op.create_index("ix_trades_asset_status", "trades", ["asset", "status"])
    op.create_index("ix_trades_opened_at", "trades", ["opened_at"])
    op.create_index("ix_trades_strategy_opened_at", "trades", ["strategy_name", "opened_at"])

    op.create_table(
        "bot_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("level", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_bot_events_event_type_created_at", "bot_events", ["event_type", "created_at"])
    op.create_index("ix_bot_events_level_created_at", "bot_events", ["level", "created_at"])

    op.create_table(
        "strategy_metrics",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("strategy_name", sa.String(length=128), nullable=False),
        sa.Column("asset", sa.String(length=64), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("signals_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("selected_signals_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trades_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wins", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("losses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("draws", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("win_rate", sa.Numeric(8, 4), nullable=False, server_default="0"),
        sa.Column("pnl", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("max_drawdown", sa.Numeric(18, 8), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("strategy_name", "asset", "date", name="uq_strategy_metrics_strategy_asset_date"),
    )


def downgrade() -> None:
    op.drop_table("strategy_metrics")
    op.drop_index("ix_bot_events_level_created_at", table_name="bot_events")
    op.drop_index("ix_bot_events_event_type_created_at", table_name="bot_events")
    op.drop_table("bot_events")
    op.drop_index("ix_trades_strategy_opened_at", table_name="trades")
    op.drop_index("ix_trades_opened_at", table_name="trades")
    op.drop_index("ix_trades_asset_status", table_name="trades")
    op.drop_table("trades")
    op.drop_index("ix_signals_candle_timestamp", table_name="signals")
    op.drop_index("ix_signals_asset_strategy_created_at", table_name="signals")
    op.drop_table("signals")
    op.drop_index("ix_candles_asset_tf_ts", table_name="candles")
    op.drop_table("candles")
    op.drop_table("settings")
    op.drop_table("telegram_users")
