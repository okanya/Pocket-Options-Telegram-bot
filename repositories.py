from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, desc, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import BalanceSnapshot, BotEvent, Candle, Setting, Signal, StrategyMetric, TelegramUser, Trade


class TelegramUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_telegram_id(self, telegram_user_id: int) -> TelegramUser | None:
        return await self.session.scalar(
            select(TelegramUser).where(TelegramUser.telegram_user_id == telegram_user_id)
        )

    async def upsert_from_allowlist(
        self,
        telegram_user_id: int,
        username: str | None = None,
        first_name: str | None = None,
        role: str = "admin",
    ) -> TelegramUser:
        stmt = (
            insert(TelegramUser)
            .values(
                telegram_user_id=telegram_user_id,
                username=username,
                first_name=first_name,
                role=role,
                is_active=True,
            )
            .on_conflict_do_update(
                constraint="uq_telegram_users_telegram_user_id",
                set_={
                    "username": username,
                    "first_name": first_name,
                    "role": role,
                    "is_active": True,
                    "updated_at": func.now(),
                },
            )
            .returning(TelegramUser)
        )
        return (await self.session.scalars(stmt)).one()

    async def is_active_admin(self, telegram_user_id: int) -> bool:
        user = await self.get_by_telegram_id(telegram_user_id)
        return bool(user and user.is_active and user.role == "admin")


class SettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, key: str) -> Setting | None:
        return await self.session.scalar(select(Setting).where(Setting.key == key))

    async def get_many(self, keys: set[str]) -> dict[str, Setting]:
        if not keys:
            return {}
        rows = await self.session.scalars(select(Setting).where(Setting.key.in_(keys)))
        return {row.key: row for row in rows}

    async def set(self, key: str, value: dict[str, Any], updated_by_telegram_user_id: int | None = None) -> Setting:
        stmt = (
            insert(Setting)
            .values(key=key, value=value, updated_by_telegram_user_id=updated_by_telegram_user_id)
            .on_conflict_do_update(
                constraint="uq_settings_key",
                set_={
                    "value": value,
                    "updated_by_telegram_user_id": updated_by_telegram_user_id,
                    "updated_at": func.now(),
                },
            )
            .returning(Setting)
        )
        return (await self.session.scalars(stmt)).one()

    async def delete(self, key: str) -> None:
        setting = await self.get(key)
        if setting is not None:
            await self.session.delete(setting)


class SecretSettingsRepository(SettingsRepository):
    POCKET_OPTION_SSID_KEY = "pocket_option_ssid"

    async def get_pocket_option_ssid(self) -> str | None:
        setting = await self.get(self.POCKET_OPTION_SSID_KEY)
        if not setting:
            return None
        value = setting.value.get("value")
        return value if isinstance(value, str) and value else None

    async def set_pocket_option_ssid(self, ssid: str, updated_by_telegram_user_id: int | None = None) -> Setting:
        return await self.set(self.POCKET_OPTION_SSID_KEY, {"value": ssid}, updated_by_telegram_user_id)

    async def clear_pocket_option_ssid(self) -> None:
        await self.delete(self.POCKET_OPTION_SSID_KEY)


class CandleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(self, **values: Any) -> Candle:
        stmt = (
            insert(Candle)
            .values(**values)
            .on_conflict_do_update(
                constraint="uq_candles_asset_tf_ts",
                set_={
                    "open": values["open"],
                    "high": values["high"],
                    "low": values["low"],
                    "close": values["close"],
                    "volume": values.get("volume"),
                    "source": values.get("source"),
                    "raw_payload": values.get("raw_payload"),
                },
            )
            .returning(Candle)
        )
        return (await self.session.scalars(stmt)).one()

    async def latest(self, asset: str, timeframe_seconds: int, limit: int) -> list[Candle]:
        stmt: Select[tuple[Candle]] = (
            select(Candle)
            .where(Candle.asset == asset, Candle.timeframe_seconds == timeframe_seconds)
            .order_by(Candle.timestamp.desc())
            .limit(limit)
        )
        return list(await self.session.scalars(stmt))


class SignalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **values: Any) -> Signal:
        signal = Signal(**values)
        self.session.add(signal)
        await self.session.flush()
        return signal

    async def count_since(self, since: datetime) -> int:
        return await self.session.scalar(select(func.count()).select_from(Signal).where(Signal.created_at >= since)) or 0


class TradeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **values: Any) -> Trade:
        trade = Trade(**values)
        self.session.add(trade)
        await self.session.flush()
        return trade

    async def update(self, trade_id: int, **values: Any) -> Trade | None:
        trade = await self.session.get(Trade, trade_id)
        if trade is None:
            return None
        for key, value in values.items():
            setattr(trade, key, value)
        await self.session.flush()
        return trade

    async def count_open(self, asset: str | None = None) -> int:
        stmt = select(func.count()).select_from(Trade).where(Trade.status == "opened")
        if asset:
            stmt = stmt.where(Trade.asset == asset)
        return await self.session.scalar(stmt) or 0

    async def open_paper_trades(self) -> list[Trade]:
        rows = await self.session.scalars(
            select(Trade)
            .options(selectinload(Trade.signal))
            .where(Trade.mode == "paper", Trade.status == "opened")
            .order_by(Trade.opened_at)
        )
        return list(rows)

    async def open_broker_trades(self) -> list[Trade]:
        rows = await self.session.scalars(
            select(Trade)
            .options(selectinload(Trade.signal))
            .where(Trade.mode.in_(("demo", "real")), Trade.status == "opened")
            .order_by(Trade.opened_at)
        )
        return list(rows)

    async def count_since(self, since: datetime, asset: str | None = None) -> int:
        stmt = select(func.count()).select_from(Trade).where(Trade.opened_at >= since)
        if asset:
            stmt = stmt.where(Trade.asset == asset)
        return await self.session.scalar(stmt) or 0

    async def count_losses_since(self, since: datetime) -> int:
        return (
            await self.session.scalar(
                select(func.count()).select_from(Trade).where(Trade.opened_at >= since, Trade.result == "loss")
            )
            or 0
        )

    async def count_same_candle_trade(self, signal_id: int | None, asset: str, candle_timestamp: datetime) -> int:
        if signal_id is not None:
            return await self.session.scalar(select(func.count()).select_from(Trade).where(Trade.signal_id == signal_id)) or 0
        return (
            await self.session.scalar(
                select(func.count())
                .select_from(Trade)
                .join(Signal, Signal.id == Trade.signal_id)
                .where(Trade.asset == asset, Signal.candle_timestamp == candle_timestamp)
            )
            or 0
        )

    async def last_closed_trade(self, asset: str | None = None) -> Trade | None:
        stmt = select(Trade).where(Trade.status == "closed").order_by(desc(Trade.closed_at), desc(Trade.opened_at)).limit(1)
        if asset:
            stmt = stmt.where(Trade.asset == asset)
        return await self.session.scalar(stmt)

    async def daily_pnl(self, day_start: datetime) -> Decimal:
        value = await self.session.scalar(select(func.coalesce(func.sum(Trade.profit), 0)).where(Trade.opened_at >= day_start))
        return Decimal(value or 0)


class BotEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        level: str,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> BotEvent:
        event = BotEvent(level=level, event_type=event_type, message=message, payload=payload)
        self.session.add(event)
        await self.session.flush()
        return event


class BalanceSnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, account_type: str, balance: Decimal, source: str, created_at: datetime | None = None) -> BalanceSnapshot:
        snapshot = BalanceSnapshot(account_type=account_type, balance=balance, source=source)
        if created_at is not None:
            snapshot.created_at = created_at
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot

    async def latest_at_or_before(self, at: datetime, account_type: str | None = None) -> BalanceSnapshot | None:
        stmt = select(BalanceSnapshot).where(BalanceSnapshot.created_at <= at).order_by(desc(BalanceSnapshot.created_at)).limit(1)
        if account_type is not None:
            stmt = stmt.where(BalanceSnapshot.account_type == account_type)
        return await self.session.scalar(stmt)


class StrategyMetricRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, strategy_name: str, asset: str, metric_date: date) -> StrategyMetric | None:
        return await self.session.scalar(
            select(StrategyMetric).where(
                StrategyMetric.strategy_name == strategy_name,
                StrategyMetric.asset == asset,
                StrategyMetric.date == metric_date,
            )
        )
