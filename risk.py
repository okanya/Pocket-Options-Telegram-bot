from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.repositories import TradeRepository
from app.trading.strategies.base import StrategySignal


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str | None = None
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RiskContext:
    signal: StrategySignal
    amount: Decimal
    payout: Decimal | None
    data_healthy: bool
    now: datetime
    signal_id: int | None = None
    admin_confirmed_trading: bool = False


@dataclass(frozen=True)
class TradeSnapshot:
    asset: str
    result: str
    opened_at: datetime
    closed_at: datetime | None


@dataclass(frozen=True)
class RiskSnapshot:
    daily_trade_count: int = 0
    asset_daily_trade_count: int = 0
    open_trades_total: int = 0
    open_trades_for_asset: int = 0
    daily_losses: int = 0
    consecutive_losses: int = 0
    daily_pnl: Decimal = Decimal("0")
    same_candle_trades: int = 0
    last_loss: TradeSnapshot | None = None
    last_asset_trade: TradeSnapshot | None = None


class RiskStats(Protocol):
    async def snapshot(self, context: RiskContext) -> RiskSnapshot:
        ...


class StaticRiskStats:
    def __init__(self, snapshot: RiskSnapshot | None = None) -> None:
        self._snapshot = snapshot or RiskSnapshot()

    async def snapshot(self, context: RiskContext) -> RiskSnapshot:
        return self._snapshot


class DatabaseRiskStats:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def snapshot(self, context: RiskContext) -> RiskSnapshot:
        day_start = context.now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        async with self.session_factory() as session:
            repo = TradeRepository(session)
            last_loss_trade = await repo.last_closed_trade()
            last_asset_trade = await repo.last_closed_trade(context.signal.asset)
            return RiskSnapshot(
                daily_trade_count=await repo.count_since(day_start),
                asset_daily_trade_count=await repo.count_since(day_start, context.signal.asset),
                open_trades_total=await repo.count_open(),
                open_trades_for_asset=await repo.count_open(context.signal.asset),
                daily_losses=await repo.count_losses_since(day_start),
                consecutive_losses=0,
                daily_pnl=await repo.daily_pnl(day_start),
                same_candle_trades=await repo.count_same_candle_trade(
                    context.signal_id,
                    context.signal.asset,
                    context.signal.candle_timestamp,
                ),
                last_loss=_trade_snapshot(last_loss_trade) if last_loss_trade and last_loss_trade.result == "loss" else None,
                last_asset_trade=_trade_snapshot(last_asset_trade) if last_asset_trade else None,
            )


class RiskManager:
    def __init__(self, settings: Settings, stats: RiskStats | None = None) -> None:
        self.settings = settings
        self.stats = stats or StaticRiskStats()

    async def evaluate(self, context: RiskContext) -> RiskDecision:
        if self.settings.signal_only:
            return _reject("signal_only_enabled")
        if not self.settings.paper_trading:
            if not self.settings.trading_enabled:
                return _reject("trading_disabled")
            if not context.admin_confirmed_trading:
                return _reject("admin_confirmation_required")
        if not context.data_healthy:
            return _reject("data_unhealthy")

        snapshot = await self.stats.snapshot(context)

        checks = (
            (snapshot.daily_trade_count >= self.settings.max_trades_total_per_day, "max_trades_total_per_day"),
            (snapshot.asset_daily_trade_count >= self.settings.max_trades_per_asset_per_day, "max_trades_per_asset_per_day"),
            (snapshot.open_trades_total >= self.settings.max_open_trades_total, "max_open_trades_total"),
            (snapshot.open_trades_for_asset >= self.settings.max_open_trades_per_asset, "max_open_trades_per_asset"),
            (snapshot.daily_losses >= self.settings.max_losses_per_day, "max_losses_per_day"),
            (snapshot.consecutive_losses >= self.settings.max_consecutive_losses, "max_consecutive_losses"),
            (snapshot.daily_pnl <= -self.settings.daily_stop_loss, "daily_stop_loss"),
            (snapshot.daily_pnl >= self.settings.daily_take_profit, "daily_take_profit"),
            (self.settings.one_trade_per_candle and snapshot.same_candle_trades > 0, "one_trade_per_candle"),
            (self.settings.one_trade_per_asset and snapshot.open_trades_for_asset > 0, "one_trade_per_asset"),
        )
        for failed, reason in checks:
            if failed:
                return _reject(reason, snapshot=snapshot)

        cooldown_decision = self._cooldown_decision(context, snapshot)
        if cooldown_decision is not None:
            return cooldown_decision

        if context.payout is not None and context.payout < self.settings.min_payout:
            return _reject("payout_below_minimum", payout=str(context.payout), min_payout=str(self.settings.min_payout))

        return RiskDecision(allowed=True, details={"reason": "approved"})

    def _cooldown_decision(self, context: RiskContext, snapshot: RiskSnapshot) -> RiskDecision | None:
        if snapshot.last_loss and snapshot.last_loss.closed_at:
            seconds_since_loss = (context.now - snapshot.last_loss.closed_at).total_seconds()
            if seconds_since_loss < self.settings.loss_cooldown_seconds:
                return _reject("loss_cooldown", remaining_seconds=self.settings.loss_cooldown_seconds - seconds_since_loss)

        if snapshot.last_asset_trade:
            reference_time = snapshot.last_asset_trade.closed_at or snapshot.last_asset_trade.opened_at
            seconds_since_asset_trade = (context.now - reference_time).total_seconds()
            if seconds_since_asset_trade < self.settings.asset_cooldown_seconds:
                return _reject(
                    "asset_cooldown",
                    remaining_seconds=self.settings.asset_cooldown_seconds - seconds_since_asset_trade,
                )

        return None


def _reject(reason: str, **details) -> RiskDecision:
    return RiskDecision(allowed=False, reason=reason, details=details)


def _trade_snapshot(trade) -> TradeSnapshot:
    return TradeSnapshot(
        asset=trade.asset,
        result=trade.result,
        opened_at=trade.opened_at,
        closed_at=trade.closed_at,
    )
