from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric
from sqlalchemy import String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TelegramUser(TimestampMixin, Base):
    __tablename__ = "telegram_users"
    __table_args__ = (
        CheckConstraint("role in ('admin', 'viewer')", name="ck_telegram_users_role"),
        UniqueConstraint("telegram_user_id", name="uq_telegram_users_telegram_user_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="viewer", server_default="viewer", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)


class Setting(TimestampMixin, Base):
    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("key", name="uq_settings_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_by_telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)


class Candle(Base):
    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("asset", "timeframe_seconds", "timestamp", name="uq_candles_asset_tf_ts"),
        Index("ix_candles_asset_tf_ts", "asset", "timeframe_seconds", "timestamp"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    asset: Mapped[str] = mapped_column(String(64), nullable=False)
    timeframe_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    volume: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    source: Mapped[str | None] = mapped_column(String(64))
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (
        CheckConstraint("direction in ('buy', 'sell')", name="ck_signals_direction"),
        CheckConstraint("mode in ('signal_only', 'paper', 'demo', 'real')", name="ck_signals_mode"),
        Index("ix_signals_asset_strategy_created_at", "asset", "strategy_name", "created_at"),
        Index("ix_signals_candle_timestamp", "candle_timestamp"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    asset: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(128), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    timeframe_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    expiration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    candle_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    reason: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    is_selected: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    is_trade_allowed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    trades: Mapped[list[Trade]] = relationship(back_populates="signal")


class Trade(TimestampMixin, Base):
    __tablename__ = "trades"
    __table_args__ = (
        CheckConstraint("direction in ('buy', 'sell')", name="ck_trades_direction"),
        CheckConstraint("mode in ('paper', 'demo', 'real')", name="ck_trades_mode"),
        CheckConstraint("result in ('win', 'loss', 'draw', 'unknown')", name="ck_trades_result"),
        CheckConstraint("status in ('opened', 'closed', 'failed', 'cancelled')", name="ck_trades_status"),
        Index("ix_trades_asset_status", "asset", "status"),
        Index("ix_trades_mode_opened_at", "mode", "opened_at"),
        Index("ix_trades_opened_at", "opened_at"),
        Index("ix_trades_strategy_opened_at", "strategy_name", "opened_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    external_trade_id: Mapped[str | None] = mapped_column(String(255))
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id", ondelete="SET NULL"))
    asset: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), default="paper", server_default="paper", nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    expiration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    open_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    close_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    result: Mapped[str] = mapped_column(String(16), default="unknown", server_default="unknown", nullable=False)
    profit: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    payout: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    signal: Mapped[Signal | None] = relationship(back_populates="trades")


class BotEvent(Base):
    __tablename__ = "bot_events"
    __table_args__ = (
        Index("ix_bot_events_event_type_created_at", "event_type", "created_at"),
        Index("ix_bot_events_level_created_at", "level", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    level: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshots"
    __table_args__ = (
        Index("ix_balance_snapshots_account_created_at", "account_type", "created_at"),
        Index("ix_balance_snapshots_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    account_type: Mapped[str] = mapped_column(String(16), nullable=False)
    balance: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class StrategyMetric(Base):
    __tablename__ = "strategy_metrics"
    __table_args__ = (UniqueConstraint("strategy_name", "asset", "date", name="uq_strategy_metrics_strategy_asset_date"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    strategy_name: Mapped[str] = mapped_column(String(128), nullable=False)
    asset: Mapped[str] = mapped_column(String(64), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    signals_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    selected_signals_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    trades_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    wins: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    losses: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    draws: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    win_rate: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=Decimal("0"), server_default="0", nullable=False)
    pnl: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"), server_default="0", nullable=False)
    max_drawdown: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
