from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from secrets import token_hex
from typing import Any, Protocol

from aiogram import BaseMiddleware, Router
from aiogram.filters import Command
from aiogram.types import Message, TelegramObject
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.analytics.stats import (
    AnalyticsService,
    format_last_trades,
    format_open_trades,
    format_paper_report,
    format_paper_status,
    format_today_stats,
    parse_stats_period,
)
from app.config import Settings
from app.db.repositories import SecretSettingsRepository, SettingsRepository, TelegramUserRepository
from app.telegram.keyboards import main_menu_keyboard
from app.trading.pocket_service import PocketConnectionService
from app.trading.strategies import UnknownStrategyError, available_strategy_names, validate_strategy_name


CONFIRMATION_TTL_SECONDS = 300
PENDING_CONFIRMATION_KEY_PREFIX = "pending_confirmation:"
DEFAULT_CLEAR_CHAT_LIMIT = 50
MAX_CLEAR_CHAT_LIMIT = 100
SLOW_SIGNALS_PRESET = {
    "signal_only": True,
    "trading_enabled": False,
    "paper_trading": False,
    "active_strategy": "all",
    "expiration_mode": "auto",
    "timeframe_seconds": 300,
    "expiration_seconds": 1800,
    "max_open_trades_total": 1,
    "max_open_trades_per_asset": 1,
    "max_trades_total_per_day": 5,
    "max_trades_per_asset_per_day": 2,
    "asset_cooldown_seconds": 1800,
    "loss_cooldown_seconds": 3600,
}


@dataclass(frozen=True)
class PendingConfirmation:
    code: str
    action: str
    value: str
    description: str
    created_at: datetime


class PendingConfirmationStore:
    def __init__(self, session_factory: async_sessionmaker) -> None:
        self.session_factory = session_factory

    async def put(
        self,
        message: Message,
        action: str,
        value: str,
        description: str,
        now: datetime | None = None,
    ) -> PendingConfirmation | None:
        pending = _create_pending_confirmation(message, action, value, description, now=now)
        if pending is None or message.from_user is None:
            return None
        async with self.session_factory() as session:
            repo = SettingsRepository(session)
            await repo.set(
                _pending_confirmation_key(message.from_user.id),
                _pending_to_setting_value(pending),
                message.from_user.id,
            )
            await session.commit()
        return pending

    async def pop(self, telegram_user_id: int, code: str, now: datetime | None = None) -> PendingConfirmation | None:
        async with self.session_factory() as session:
            repo = SettingsRepository(session)
            setting = await repo.get(_pending_confirmation_key(telegram_user_id))
            pending = _pending_from_setting_value(setting.value if setting else None)
            if pending is None:
                return None
            if _is_confirmation_expired(pending, now=now):
                await repo.delete(_pending_confirmation_key(telegram_user_id))
                await session.commit()
                return None
            if not code or code != pending.code:
                return None
            await repo.delete(_pending_confirmation_key(telegram_user_id))
            await session.commit()
            return pending

    async def cancel(self, telegram_user_id: int) -> bool:
        async with self.session_factory() as session:
            repo = SettingsRepository(session)
            setting = await repo.get(_pending_confirmation_key(telegram_user_id))
            if setting is None:
                return False
            await repo.delete(_pending_confirmation_key(telegram_user_id))
            await session.commit()
            return True


class EngineStatusProvider(Protocol):
    def status(self) -> Any:
        ...

    def set_strategy(self, strategy_name: str) -> None:
        ...

    def set_assets(self, assets: list[str]) -> None:
        ...


class AllowlistMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings, session_factory: async_sessionmaker) -> None:
        self.settings = settings
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.from_user is None:
            return await handler(event, data)

        telegram_user_id = event.from_user.id
        if telegram_user_id not in self.settings.telegram_admin_ids:
            await event.answer("Access denied.")
            return None

        async with self.session_factory() as session:
            repo = TelegramUserRepository(session)
            await repo.upsert_from_allowlist(
                telegram_user_id=telegram_user_id,
                username=event.from_user.username,
                first_name=event.from_user.first_name,
                role="admin",
            )
            await session.commit()

        return await handler(event, data)


def build_router(
    settings: Settings,
    session_factory: async_sessionmaker,
    pocket_service: PocketConnectionService | None = None,
    engine_status_provider: EngineStatusProvider | None = None,
) -> Router:
    router = Router()
    router.message.middleware(AllowlistMiddleware(settings, session_factory))
    pocket_service = pocket_service or PocketConnectionService(session_factory, account_type=settings.qt_account_mode)
    pending_store = PendingConfirmationStore(session_factory)

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        await message.answer(
            "Pocket Option bot\nMode: signal_only\nTrading: disabled",
            reply_markup=main_menu_keyboard(),
        )

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "/start - main menu\n"
            "/status - bot status\n"
            "/settings - current settings\n"
            "/ssid_status - PocketOption SSID status\n"
            "/set_ssid <ssid> - save PocketOption SSID\n"
            "/clear_ssid - remove PocketOption SSID\n"
            "/balance - PocketOption balance\n"
            "/assets - available PocketOption assets with payout\n"
            "/qt_demo - select QT demo account mode\n"
            "/qt_real - select QT real account mode after confirmation\n"
            "/strategies - available strategies\n"
            "/set_strategy <name> - select active strategy\n"
            "/slow_signals - safe 5m candles / auto expiration preset\n"
            "/set_assets <csv> - set active assets\n"
            "/set_expiration <seconds> - set expiration\n"
            "/set_expiration_auto - choose expiration by strategy/timeframe\n"
            "/set_expiration_fixed <seconds> - use fixed expiration\n"
            "/set_timeframe <seconds> - set timeframe\n"
            "/set_amount <decimal> - set trade amount, increases require confirmation\n"
            "/set_min_payout <decimal> - set min payout\n"
            "/limits - current risk limits\n"
            "/set_max_trades_day <count> - set daily trade limit\n"
            "/set_max_trades_asset <count> - set per-asset daily trade limit\n"
            "/set_max_open_trades <count> - set max open trades\n"
            "/set_max_open_asset <count> - set per-asset open trade limit\n"
            "/set_max_losses_day <count> - set daily loss limit\n"
            "/set_max_consecutive_losses <count> - set consecutive loss limit\n"
            "/set_daily_stop_loss <decimal> - set daily stop loss\n"
            "/set_daily_take_profit <decimal> - set daily take profit\n"
            "/set_loss_cooldown <seconds> - set cooldown after loss\n"
            "/set_asset_cooldown <seconds> - set per-asset cooldown\n"
            "/stats [1h|12h|1d|2d] - statistics for today or period\n"
            "/open_trades - open trades\n"
            "/last_trades - latest trades\n"
            "/clear_chat [count] - delete recent Telegram messages in this chat\n"
            "/paper_trading - enable paper trading\n"
            "/paper_status - paper trading status\n"
            "/paper_report - paper-only report\n"
            "/stop_paper - stop paper trading\n"
            "/start_demo_trading - enable QT demo trading after confirmation\n"
            "/start_trading - enable QT real trading after confirmation and unlock\n"
            "/confirm <code> - confirm pending dangerous action\n"
            "/cancel - cancel pending dangerous action\n"
            "/signal_only - enable signal-only mode\n"
            "/stop_trading - disable trading\n"
            "/help - command help"
        )

    @router.message(Command("clear_chat"))
    @router.message(Command("clear_history"))
    async def clear_chat(message: Message) -> None:
        limit = _parse_clear_chat_limit(_extract_command_argument(message.text))
        if limit is None:
            await message.answer(f"Usage: /clear_chat 50\nMax: {MAX_CLEAR_CHAT_LIMIT}")
            return
        deleted = await _delete_recent_chat_messages(message, limit)
        result = await message.answer(f"Chat cleanup requested. Deleted: {deleted}.")
        await asyncio.sleep(5)
        await _delete_message_quietly(result)

    @router.message(Command("status"))
    async def status(message: Message) -> None:
        pocket_status = await pocket_service.status()
        engine_status = engine_status_provider.status() if engine_status_provider is not None else None
        try:
            open_trades_count = len(await AnalyticsService(session_factory).open_trades())
        except Exception:
            open_trades_count = None
        await message.answer(_format_status(settings, pocket_status, engine_status, open_trades_count))

    @router.message(Command("settings"))
    async def settings_command(message: Message) -> None:
        await message.answer(
            "Current settings\n"
            f"Signal only: {settings.signal_only}\n"
            f"Trading enabled: {settings.trading_enabled}\n"
            f"QT account: {settings.qt_account_mode}\n"
            f"Real trading unlocked: {settings.real_trading_unlocked}\n"
            f"Real trading confirmed: {settings.real_trading_admin_confirmed}\n"
            f"Paper trading: {settings.paper_trading}\n"
            f"Amount: {settings.trade_amount}\n"
            f"Expiration mode: {settings.expiration_mode}\n"
            f"Expiration: {settings.expiration_seconds}s\n"
            f"Timeframe: {settings.timeframe_seconds}s\n"
            f"Assets: {', '.join(settings.active_assets)}\n"
            f"Strategy: {settings.active_strategy}\n"
            f"Max open trades total: {settings.max_open_trades_total}\n"
            f"Max open trades per asset: {settings.max_open_trades_per_asset}\n"
            f"Max trades per day: {settings.max_trades_total_per_day}\n"
            f"Max trades per asset per day: {settings.max_trades_per_asset_per_day}\n"
            f"Daily stop loss: {settings.daily_stop_loss}\n"
            f"Daily take profit: {settings.daily_take_profit}\n"
            f"Min payout: {settings.min_payout}"
        )

    @router.message(Command("strategies"))
    async def strategies(message: Message) -> None:
        await message.answer(_format_available_strategies())

    @router.message(Command("set_strategy"))
    async def set_strategy(message: Message) -> None:
        strategy_name = _extract_command_argument(message.text)
        try:
            validate_strategy_name(strategy_name)
        except UnknownStrategyError:
            await message.answer(
                "Unknown strategy.\n"
                f"{_format_available_strategies()}"
            )
            return

        async with session_factory() as session:
            repo = SettingsRepository(session)
            await repo.set("active_strategy", {"value": strategy_name}, message.from_user.id if message.from_user else None)
            await session.commit()
        settings.active_strategy = strategy_name
        if engine_status_provider is not None:
            engine_status_provider.set_strategy(strategy_name)
        await message.answer(f"Active strategy set to {strategy_name}.")

    @router.message(Command("sets_assets"))
    @router.message(Command("set_assets"))
    async def set_assets(message: Message) -> None:
        assets = _parse_assets_argument(_extract_command_argument(message.text))
        if not assets:
            await message.answer("Usage: /set_assets EURUSD_otc,GBPUSD_otc")
            return
        await _save_runtime_setting(session_factory, "active_assets", assets, message.from_user.id if message.from_user else None)
        settings.active_assets = assets
        if engine_status_provider is not None:
            engine_status_provider.set_assets(assets)
        await message.answer(f"Active assets set to: {', '.join(assets)}.")

    @router.message(Command("set_expiration"))
    async def set_expiration(message: Message) -> None:
        value = _parse_positive_int_argument(_extract_command_argument(message.text))
        if value is None:
            await message.answer("Usage: /set_expiration 60")
            return
        await _save_runtime_setting(session_factory, "expiration_seconds", value, message.from_user.id if message.from_user else None)
        await _save_runtime_setting(session_factory, "expiration_mode", "fixed", message.from_user.id if message.from_user else None)
        settings.expiration_seconds = value
        settings.expiration_mode = "fixed"
        await message.answer(f"Expiration set to {value}s.")

    @router.message(Command("set_expiration_auto"))
    async def set_expiration_auto(message: Message) -> None:
        await _save_runtime_setting(session_factory, "expiration_mode", "auto", message.from_user.id if message.from_user else None)
        settings.expiration_mode = "auto"
        await message.answer("Expiration mode set to auto.")

    @router.message(Command("set_expiration_fixed"))
    async def set_expiration_fixed(message: Message) -> None:
        value = _parse_positive_int_argument(_extract_command_argument(message.text))
        if value is None:
            await message.answer("Usage: /set_expiration_fixed 60")
            return
        await _save_runtime_settings(
            session_factory,
            settings,
            {"expiration_mode": "fixed", "expiration_seconds": value},
            message.from_user.id if message.from_user else None,
        )
        await message.answer(f"Expiration mode set to fixed: {value}s.")

    @router.message(Command("set_timeframe"))
    async def set_timeframe(message: Message) -> None:
        value = _parse_positive_int_argument(_extract_command_argument(message.text))
        if value is None:
            await message.answer("Usage: /set_timeframe 60")
            return
        await _save_runtime_setting(session_factory, "timeframe_seconds", value, message.from_user.id if message.from_user else None)
        settings.timeframe_seconds = value
        await message.answer(f"Timeframe set to {value}s. Restart or reconnect the engine to apply it.")

    @router.message(Command("slow_signals"))
    async def slow_signals(message: Message) -> None:
        values = _slow_signals_preset_values()
        await _save_runtime_settings(
            session_factory,
            settings,
            values,
            message.from_user.id if message.from_user else None,
        )
        if engine_status_provider is not None:
            engine_status_provider.set_strategy(str(values["active_strategy"]))
            engine_status_provider.set_assets(settings.active_assets)
        await message.answer(
            "Slow signals preset enabled\n"
            "Mode: signal_only\n"
            "Strategy: all\n"
            "Timeframe: 300s\n"
            "Expiration mode: auto\n"
            "Expiration: 10m / 15m / 30m by strategy\n"
            "Trading: disabled"
        )

    @router.message(Command("set_min_payout"))
    async def set_min_payout(message: Message) -> None:
        value = _parse_positive_decimal_argument(_extract_command_argument(message.text))
        if value is None:
            await message.answer("Usage: /set_min_payout 0.75")
            return
        await _save_runtime_setting(session_factory, "min_payout", str(value), message.from_user.id if message.from_user else None)
        settings.min_payout = value
        await message.answer(f"Min payout set to {value}.")

    @router.message(Command("set_amount"))
    async def set_amount(message: Message) -> None:
        value = _parse_positive_decimal_argument(_extract_command_argument(message.text))
        if value is None:
            await message.answer("Usage: /set_amount 1")
            return
        if value > settings.trade_amount:
            pending = await pending_store.put(
                message,
                action="increase_amount",
                value=str(value),
                description=f"Increase trade amount from {settings.trade_amount} to {value}",
            )
            if pending is None:
                await message.answer("Cannot create confirmation without Telegram user.")
                return
            await message.answer(_format_confirmation_request(pending))
            return
        await _save_runtime_setting(session_factory, "trade_amount", str(value), message.from_user.id if message.from_user else None)
        settings.trade_amount = value
        await message.answer(f"Trade amount set to {value}.")

    @router.message(Command("limits"))
    async def limits(message: Message) -> None:
        await message.answer(
            "Risk limits\n"
            f"Max trades per day: {settings.max_trades_total_per_day}\n"
            f"Max trades per asset per day: {settings.max_trades_per_asset_per_day}\n"
            f"Max open trades total: {settings.max_open_trades_total}\n"
            f"Max open trades per asset: {settings.max_open_trades_per_asset}\n"
            f"Max losses per day: {settings.max_losses_per_day}\n"
            f"Max consecutive losses: {settings.max_consecutive_losses}\n"
            f"Daily stop loss: {settings.daily_stop_loss}\n"
            f"Daily take profit: {settings.daily_take_profit}\n"
            f"Loss cooldown: {settings.loss_cooldown_seconds}s\n"
            f"Asset cooldown: {settings.asset_cooldown_seconds}s"
        )

    @router.message(Command("set_max_trades_day"))
    async def set_max_trades_day(message: Message) -> None:
        value = _parse_positive_int_argument(_extract_command_argument(message.text))
        if value is None:
            await message.answer("Usage: /set_max_trades_day 5")
            return
        await _save_runtime_setting(
            session_factory,
            "max_trades_total_per_day",
            value,
            message.from_user.id if message.from_user else None,
        )
        settings.max_trades_total_per_day = value
        await message.answer(f"Max trades per day set to {value}.")

    @router.message(Command("set_max_trades_asset"))
    async def set_max_trades_asset(message: Message) -> None:
        value = _parse_positive_int_argument(_extract_command_argument(message.text))
        if value is None:
            await message.answer("Usage: /set_max_trades_asset 1")
            return
        await _set_int_runtime_setting(
            session_factory,
            settings,
            message,
            key="max_trades_per_asset_per_day",
            value=value,
            response=f"Max trades per asset per day set to {value}.",
        )

    @router.message(Command("set_max_open_trades"))
    async def set_max_open_trades(message: Message) -> None:
        value = _parse_positive_int_argument(_extract_command_argument(message.text))
        if value is None:
            await message.answer("Usage: /set_max_open_trades 1")
            return
        await _save_runtime_setting(
            session_factory,
            "max_open_trades_total",
            value,
            message.from_user.id if message.from_user else None,
        )
        settings.max_open_trades_total = value
        await message.answer(f"Max open trades total set to {value}.")

    @router.message(Command("set_max_open_asset"))
    async def set_max_open_asset(message: Message) -> None:
        value = _parse_positive_int_argument(_extract_command_argument(message.text))
        if value is None:
            await message.answer("Usage: /set_max_open_asset 1")
            return
        await _set_int_runtime_setting(
            session_factory,
            settings,
            message,
            key="max_open_trades_per_asset",
            value=value,
            response=f"Max open trades per asset set to {value}.",
        )

    @router.message(Command("set_max_losses_day"))
    async def set_max_losses_day(message: Message) -> None:
        value = _parse_positive_int_argument(_extract_command_argument(message.text))
        if value is None:
            await message.answer("Usage: /set_max_losses_day 2")
            return
        await _set_int_runtime_setting(
            session_factory,
            settings,
            message,
            key="max_losses_per_day",
            value=value,
            response=f"Max losses per day set to {value}.",
        )

    @router.message(Command("set_max_consecutive_losses"))
    async def set_max_consecutive_losses(message: Message) -> None:
        value = _parse_positive_int_argument(_extract_command_argument(message.text))
        if value is None:
            await message.answer("Usage: /set_max_consecutive_losses 2")
            return
        await _set_int_runtime_setting(
            session_factory,
            settings,
            message,
            key="max_consecutive_losses",
            value=value,
            response=f"Max consecutive losses set to {value}.",
        )

    @router.message(Command("set_daily_stop_loss"))
    async def set_daily_stop_loss(message: Message) -> None:
        value = _parse_positive_decimal_argument(_extract_command_argument(message.text))
        if value is None:
            await message.answer("Usage: /set_daily_stop_loss 10")
            return
        await _set_decimal_runtime_setting(
            session_factory,
            settings,
            message,
            key="daily_stop_loss",
            value=value,
            response=f"Daily stop loss set to {value}.",
        )

    @router.message(Command("set_daily_take_profit"))
    async def set_daily_take_profit(message: Message) -> None:
        value = _parse_positive_decimal_argument(_extract_command_argument(message.text))
        if value is None:
            await message.answer("Usage: /set_daily_take_profit 20")
            return
        await _set_decimal_runtime_setting(
            session_factory,
            settings,
            message,
            key="daily_take_profit",
            value=value,
            response=f"Daily take profit set to {value}.",
        )

    @router.message(Command("set_loss_cooldown"))
    async def set_loss_cooldown(message: Message) -> None:
        value = _parse_positive_int_argument(_extract_command_argument(message.text))
        if value is None:
            await message.answer("Usage: /set_loss_cooldown 900")
            return
        await _set_int_runtime_setting(
            session_factory,
            settings,
            message,
            key="loss_cooldown_seconds",
            value=value,
            response=f"Loss cooldown set to {value}s.",
        )

    @router.message(Command("set_asset_cooldown"))
    async def set_asset_cooldown(message: Message) -> None:
        value = _parse_positive_int_argument(_extract_command_argument(message.text))
        if value is None:
            await message.answer("Usage: /set_asset_cooldown 300")
            return
        await _set_int_runtime_setting(
            session_factory,
            settings,
            message,
            key="asset_cooldown_seconds",
            value=value,
            response=f"Asset cooldown set to {value}s.",
        )

    @router.message(Command("ssid_status"))
    async def ssid_status(message: Message) -> None:
        async with session_factory() as session:
            repo = SecretSettingsRepository(session)
            ssid = await repo.get_pocket_option_ssid()
        await message.answer(f"PocketOption SSID: {_mask_secret(ssid) if ssid else 'not set'}")

    @router.message(Command("set_ssid"))
    async def set_ssid(message: Message) -> None:
        ssid = _extract_command_argument(message.text)
        if not ssid:
            await message.answer("Usage: /set_ssid <ssid>")
            return
        if not _looks_like_pocket_option_ssid(ssid):
            await _delete_message_quietly(message)
            await message.answer(
                "Invalid PocketOption SSID format.\n"
                'Expected either a raw browser session token or 42["auth",{"session":"...","isDemo":1,"uid":...}]'
            )
            return

        async with session_factory() as session:
            repo = SecretSettingsRepository(session)
            current_ssid = await repo.get_pocket_option_ssid()
            normalized_ssid = _normalize_pocket_option_ssid(ssid, current_ssid)
            await repo.set_pocket_option_ssid(
                normalized_ssid,
                message.from_user.id if message.from_user else None,
            )
            await session.commit()

        await _delete_message_quietly(message)
        await message.answer(f"PocketOption SSID saved: {_mask_secret(normalized_ssid)}")

    @router.message(Command("clear_ssid"))
    async def clear_ssid(message: Message) -> None:
        async with session_factory() as session:
            repo = SecretSettingsRepository(session)
            await repo.clear_pocket_option_ssid()
            await session.commit()
        await message.answer("PocketOption SSID cleared.")

    @router.message(Command("balance"))
    async def balance(message: Message) -> None:
        if engine_status_provider is not None:
            engine_status = engine_status_provider.status()
            if not getattr(engine_status, "connected", False):
                pocket_status = await pocket_service.status()
                if not pocket_status.ssid_configured:
                    await message.answer("PocketOption SSID is not set. Use /set_ssid <ssid> first.")
                    return
                await message.answer(
                    "PocketOption is not connected now. Balance check skipped to avoid a 60s connection timeout."
                )
                return

        result = await pocket_service.get_balance()
        if not result.status.ssid_configured:
            await message.answer("PocketOption SSID is not set. Use /set_ssid <ssid> first.")
            return
        if result.status.error:
            await message.answer(f"PocketOption balance error: {result.status.error}")
            return
        await message.answer(f"PocketOption balance: {result.balance if result.balance is not None else 'unknown'}")

    @router.message(Command("assets"))
    async def assets(message: Message) -> None:
        result = await pocket_service.get_assets()
        if not result.status.ssid_configured:
            await message.answer("PocketOption SSID is not set. Use /set_ssid <ssid> first.")
            return
        if result.status.error:
            await message.answer(f"PocketOption assets error: {result.status.error}")
            return
        for chunk in _format_available_assets_chunks(result.payouts, settings.active_assets):
            await message.answer(chunk)

    @router.message(Command("qt_demo"))
    async def qt_demo(message: Message) -> None:
        await _save_runtime_setting(session_factory, "qt_account_mode", "demo", message.from_user.id if message.from_user else None)
        await _save_runtime_setting(
            session_factory,
            "real_trading_admin_confirmed",
            False,
            message.from_user.id if message.from_user else None,
        )
        settings.qt_account_mode = "demo"
        settings.real_trading_admin_confirmed = False
        await message.answer("QT account mode set to demo.")

    @router.message(Command("qt_real"))
    async def qt_real(message: Message) -> None:
        pending = await pending_store.put(
            message,
            action="qt_real",
            value="real",
            description="Switch QT account mode to real. Real trading still remains disabled.",
        )
        if pending is None:
            await message.answer("Cannot create confirmation without Telegram user.")
            return
        await message.answer(_format_confirmation_request(pending))

    @router.message(Command("stats"))
    async def stats(message: Message) -> None:
        service = AnalyticsService(session_factory)
        period_arg = _extract_command_argument(message.text)
        if not period_arg:
            await message.answer(format_today_stats(await service.today_stats(account_type=settings.qt_account_mode)))
            return
        period = parse_stats_period(period_arg)
        if period is None:
            await message.answer("Usage: /stats [1h|12h|1d|2d]")
            return
        await message.answer(
            format_today_stats(
                await service.stats_since(period.start_at, account_type=settings.qt_account_mode),
                title=f"Stats {period.label}",
            )
        )

    @router.message(Command("last_trades"))
    async def last_trades(message: Message) -> None:
        service = AnalyticsService(session_factory)
        await message.answer(format_last_trades(await service.last_trades(limit=10)))

    @router.message(Command("open_trades"))
    async def open_trades(message: Message) -> None:
        service = AnalyticsService(session_factory)
        await message.answer(format_open_trades(await service.open_trades()))

    @router.message(Command("paper_trading"))
    async def paper_trading(message: Message) -> None:
        pending = await pending_store.put(
            message,
            action="enable_paper",
            value="true",
            description="Disable signal-only mode and enable paper trading. Real trading remains disabled.",
        )
        if pending is None:
            await message.answer("Cannot create confirmation without Telegram user.")
            return
        await message.answer(_format_confirmation_request(pending))

    @router.message(Command("confirm"))
    async def confirm(message: Message) -> None:
        if message.from_user is None:
            await message.answer("Cannot confirm without Telegram user.")
            return
        code = _extract_command_argument(message.text)
        pending = await pending_store.pop(message.from_user.id, code)
        if pending is None:
            await message.answer("No matching pending confirmation.")
            return
        if pending.action == "increase_amount":
            value = Decimal(pending.value)
            await _save_runtime_setting(session_factory, "trade_amount", str(value), message.from_user.id)
            settings.trade_amount = value
            await message.answer(f"Confirmed. Trade amount set to {value}.")
            return
        if pending.action == "qt_real":
            await _save_runtime_setting(session_factory, "qt_account_mode", "real", message.from_user.id)
            await _save_runtime_setting(session_factory, "real_trading_admin_confirmed", False, message.from_user.id)
            settings.qt_account_mode = "real"
            settings.real_trading_admin_confirmed = False
            await message.answer("Confirmed. QT account mode set to real. Trading remains disabled.")
            return
        if pending.action == "enable_paper":
            await _enable_paper_trading(session_factory, settings, message.from_user.id)
            await message.answer("Confirmed. Paper trading enabled. Real trading remains disabled.")
            return
        if pending.action == "enable_demo_trading":
            await _enable_demo_trading(session_factory, settings, message.from_user.id)
            await message.answer("Confirmed. QT demo trading enabled. Real trading remains disabled.")
            return
        if pending.action == "enable_real_trading":
            if not settings.real_trading_unlocked:
                await message.answer("Confirmed, but QT Real trading remains locked by REAL_TRADING_UNLOCKED=false.")
                return
            await _enable_real_trading(session_factory, settings, message.from_user.id)
            await message.answer("Confirmed. QT Real trading enabled.")
            return
        await message.answer("Unknown confirmation action.")

    @router.message(Command("cancel"))
    async def cancel(message: Message) -> None:
        if message.from_user is None:
            await message.answer("Cannot cancel without Telegram user.")
            return
        removed = await pending_store.cancel(message.from_user.id)
        await message.answer("Pending confirmation cancelled." if removed else "No pending confirmation.")

    @router.message(Command("start_trading"))
    async def start_trading(message: Message) -> None:
        if settings.qt_account_mode != "real":
            await message.answer("Select QT Real first with /qt_real, then confirm /start_trading.")
            return
        pending = await pending_store.put(
            message,
            action="enable_real_trading",
            value="true",
            description="Request QT Real trading. Engine-level real execution remains blocked unless explicitly unlocked.",
        )
        if pending is None:
            await message.answer("Cannot create confirmation without Telegram user.")
            return
        await message.answer(_format_confirmation_request(pending))

    @router.message(Command("start_demo_trading"))
    async def start_demo_trading(message: Message) -> None:
        pending = await pending_store.put(
            message,
            action="enable_demo_trading",
            value="true",
            description="Disable signal-only mode and enable QT demo trading. Real trading remains disabled.",
        )
        if pending is None:
            await message.answer("Cannot create confirmation without Telegram user.")
            return
        await message.answer(_format_confirmation_request(pending))

    @router.message(Command("paper_status"))
    async def paper_status(message: Message) -> None:
        service = AnalyticsService(session_factory)
        open_trades = [trade for trade in await service.open_trades() if trade.mode == "paper"]
        stats = await service.today_stats(mode="paper")
        await message.answer(
            format_paper_status(
                paper_enabled=settings.paper_trading,
                open_trades=open_trades,
                stats=stats,
                assets=settings.active_assets,
                strategy=settings.active_strategy,
            )
        )

    @router.message(Command("paper_report"))
    async def paper_report(message: Message) -> None:
        service = AnalyticsService(session_factory)
        await message.answer(format_paper_report(await service.today_stats(mode="paper")))

    @router.message(Command("stop_paper"))
    async def stop_paper(message: Message) -> None:
        async with session_factory() as session:
            repo = SettingsRepository(session)
            await repo.set("paper_trading", {"value": False}, message.from_user.id if message.from_user else None)
            await repo.set("signal_only", {"value": True}, message.from_user.id if message.from_user else None)
            await repo.set("trading_enabled", {"value": False}, message.from_user.id if message.from_user else None)
            await repo.set("real_trading_admin_confirmed", {"value": False}, message.from_user.id if message.from_user else None)
            await session.commit()
        settings.paper_trading = False
        settings.signal_only = True
        settings.trading_enabled = False
        settings.real_trading_admin_confirmed = False
        await message.answer("Paper trading stopped. Signal-only mode enabled.")

    @router.message(Command("signal_only"))
    async def signal_only(message: Message) -> None:
        async with session_factory() as session:
            repo = SettingsRepository(session)
            await repo.set("signal_only", {"value": True}, message.from_user.id if message.from_user else None)
            await repo.set("paper_trading", {"value": False}, message.from_user.id if message.from_user else None)
            await repo.set("trading_enabled", {"value": False}, message.from_user.id if message.from_user else None)
            await repo.set("real_trading_admin_confirmed", {"value": False}, message.from_user.id if message.from_user else None)
            await session.commit()
        settings.signal_only = True
        settings.paper_trading = False
        settings.trading_enabled = False
        settings.real_trading_admin_confirmed = False
        await message.answer("Signal-only mode enabled. Trading disabled.")

    @router.message(Command("stop_trading"))
    async def stop_trading(message: Message) -> None:
        async with session_factory() as session:
            repo = SettingsRepository(session)
            await repo.set("trading_enabled", {"value": False}, message.from_user.id if message.from_user else None)
            await repo.set("real_trading_admin_confirmed", {"value": False}, message.from_user.id if message.from_user else None)
            await session.commit()
        settings.trading_enabled = False
        settings.real_trading_admin_confirmed = False
        await message.answer("Trading disabled.")

    return router


def _extract_command_argument(text: str | None) -> str:
    if not text:
        return ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


def _mask_secret(secret: str | None) -> str:
    if not secret:
        return "not set"
    if len(secret) <= 8:
        return "***"
    return f"{secret[:4]}...{secret[-4:]}"


def _looks_like_pocket_option_auth_ssid(ssid: str) -> bool:
    if not ssid.startswith("42["):
        return False
    try:
        payload = json.loads(ssid[2:])
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, list) or len(payload) < 2:
        return False
    event_name, auth_payload = payload[0], payload[1]
    if event_name != "auth" or not isinstance(auth_payload, dict):
        return False
    return (
        isinstance(auth_payload.get("session"), str)
        and auth_payload["session"] != ""
        and "isDemo" in auth_payload
        and "uid" in auth_payload
    )


def _looks_like_pocket_option_cookie_ssid(ssid: str) -> bool:
    if not ssid or ssid.startswith("42["):
        return False
    if any(ch.isspace() for ch in ssid):
        return False
    return 16 <= len(ssid) <= 256


def _looks_like_pocket_option_ssid(ssid: str) -> bool:
    return _looks_like_pocket_option_auth_ssid(ssid) or _looks_like_pocket_option_cookie_ssid(ssid)


def _normalize_pocket_option_ssid(ssid: str, current_ssid: str | None = None) -> str:
    if _looks_like_pocket_option_auth_ssid(ssid):
        return ssid
    auth_payload = _auth_payload_from_ssid(current_ssid) or {}
    normalized_payload = {
        "session": ssid,
        "isDemo": auth_payload.get("isDemo", 1),
        "uid": auth_payload.get("uid", 0),
        "platform": auth_payload.get("platform", 2),
        "isFastHistory": auth_payload.get("isFastHistory", True),
        "isOptimized": auth_payload.get("isOptimized", True),
    }
    return json.dumps(["auth", normalized_payload], separators=(",", ":")).join(("42", ""))


def _auth_payload_from_ssid(ssid: str | None) -> dict[str, Any] | None:
    if not ssid or not ssid.startswith("42["):
        return None
    try:
        payload = json.loads(ssid[2:])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list) or len(payload) < 2:
        return None
    if payload[0] != "auth" or not isinstance(payload[1], dict):
        return None
    return payload[1]


def _format_available_strategies() -> str:
    return "Available strategies\n" + "\n".join(f"- {name}" for name in available_strategy_names())


def _format_available_assets(
    payouts: dict[str, Decimal | None],
    active_assets: list[str],
    limit: int = 80,
) -> str:
    if not payouts:
        return "Available assets: none"

    active_set = set(active_assets)
    rows = sorted(
        payouts.items(),
        key=lambda item: (item[1] is not None, item[1] or Decimal("-1"), item[0]),
        reverse=True,
    )
    lines = [
        f"Available assets: {len(payouts)}",
        f"Active assets: {', '.join(active_assets) if active_assets else 'none'}",
        "",
    ]
    for asset, payout in rows[:limit]:
        marker = "*" if asset in active_set else "-"
        payout_text = f"{payout * 100:.0f}%" if payout is not None else "unknown"
        lines.append(f"{marker} {asset}: {payout_text}")
    if len(rows) > limit:
        lines.append(f"...and {len(rows) - limit} more")
    return "\n".join(lines)


def _format_available_assets_chunks(
    payouts: dict[str, Decimal | None],
    active_assets: list[str],
    page_size: int = 60,
) -> list[str]:
    if not payouts:
        return ["Available assets: none"]

    active_set = set(active_assets)
    rows = sorted(
        payouts.items(),
        key=lambda item: (item[1] is not None, item[1] or Decimal("-1"), item[0]),
        reverse=True,
    )
    chunks: list[str] = []
    total_pages = (len(rows) + page_size - 1) // page_size
    for page_index, start in enumerate(range(0, len(rows), page_size), start=1):
        lines = [
            f"Available assets: {len(payouts)}",
            f"Active assets: {', '.join(active_assets) if active_assets else 'none'}",
            f"Page: {page_index}/{total_pages}",
            "",
        ]
        for asset, payout in rows[start : start + page_size]:
            marker = "*" if asset in active_set else "-"
            payout_text = f"{payout * 100:.0f}%" if payout is not None else "unknown"
            lines.append(f"{marker} {asset}: {payout_text}")
        chunks.append("\n".join(lines))
    return chunks


def _format_status(settings: Settings, pocket_status: Any, engine_status: Any | None, open_trades_count: int | None) -> str:
    engine_running = getattr(engine_status, "running", False) if engine_status is not None else False
    engine_connected = getattr(engine_status, "connected", False) if engine_status is not None else False
    engine_open_trades = getattr(engine_status, "open_trades", None) if engine_status is not None else None
    if open_trades_count is None:
        open_trades = engine_open_trades if engine_open_trades is not None else "unknown"
    else:
        open_trades = open_trades_count

    pocket_connection = "connected" if (engine_connected or getattr(pocket_status, "connected", False)) else "not connected"
    if getattr(pocket_status, "mocked", False):
        pocket_connection = "mocked"
    if getattr(pocket_status, "error", None):
        pocket_connection = f"error: {pocket_status.error}"

    return (
        f"Status: {'running' if engine_running else 'idle'}\n"
        f"Engine connected: {engine_connected}\n"
        f"Mode: {settings.safe_mode_name}\n"
        f"QT account: {settings.qt_account_mode}\n"
        f"Real trading unlocked: {settings.real_trading_unlocked}\n"
        f"Real trading confirmed: {settings.real_trading_admin_confirmed}\n"
        f"Paper trading: {settings.paper_trading}\n"
        f"Signal only: {settings.signal_only}\n"
        f"Trading enabled: {settings.trading_enabled}\n"
        f"Assets: {', '.join(settings.active_assets)}\n"
        f"Strategy: {settings.active_strategy}\n"
        f"PocketOption SSID: {'set' if getattr(pocket_status, 'ssid_configured', False) else 'not set'}\n"
        f"PocketOption: {pocket_connection}\n"
        f"Open trades: {open_trades}"
    )


def _parse_assets_argument(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_positive_int_argument(value: str) -> int | None:
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _parse_positive_decimal_argument(value: str) -> Decimal | None:
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed > 0 else None


def _parse_clear_chat_limit(value: str) -> int | None:
    if not value:
        return DEFAULT_CLEAR_CHAT_LIMIT
    parsed = _parse_positive_int_argument(value)
    if parsed is None or parsed > MAX_CLEAR_CHAT_LIMIT:
        return None
    return parsed


def _put_pending_confirmation(
    pending_confirmations: dict[int, PendingConfirmation],
    message: Message,
    action: str,
    value: str,
    description: str,
    now: datetime | None = None,
) -> PendingConfirmation | None:
    if message.from_user is None:
        return None
    pending = PendingConfirmation(
        code=token_hex(3),
        action=action,
        value=value,
        description=description,
        created_at=now or datetime.now(UTC),
    )
    pending_confirmations[message.from_user.id] = pending
    return pending


def _pop_pending_confirmation(
    pending_confirmations: dict[int, PendingConfirmation],
    telegram_user_id: int,
    code: str,
    now: datetime | None = None,
) -> PendingConfirmation | None:
    pending = pending_confirmations.get(telegram_user_id)
    if pending is None:
        return None
    if _is_confirmation_expired(pending, now=now):
        pending_confirmations.pop(telegram_user_id, None)
        return None
    if not code or code != pending.code:
        return None
    return pending_confirmations.pop(telegram_user_id)


def _is_confirmation_expired(pending: PendingConfirmation, now: datetime | None = None) -> bool:
    return (now or datetime.now(UTC)) - pending.created_at > timedelta(seconds=CONFIRMATION_TTL_SECONDS)


def _format_confirmation_request(pending: PendingConfirmation) -> str:
    return (
        "Confirmation required\n"
        f"Action: {pending.description}\n"
        f"Confirm: /confirm {pending.code}\n"
        "Cancel: /cancel\n"
        f"Expires in: {CONFIRMATION_TTL_SECONDS // 60} minutes"
    )


def _create_pending_confirmation(
    message: Message,
    action: str,
    value: str,
    description: str,
    now: datetime | None = None,
) -> PendingConfirmation | None:
    if message.from_user is None:
        return None
    return PendingConfirmation(
        code=token_hex(3),
        action=action,
        value=value,
        description=description,
        created_at=now or datetime.now(UTC),
    )


def _pending_confirmation_key(telegram_user_id: int) -> str:
    return f"{PENDING_CONFIRMATION_KEY_PREFIX}{telegram_user_id}"


def _pending_to_setting_value(pending: PendingConfirmation) -> dict[str, Any]:
    return {
        "value": {
            "code": pending.code,
            "action": pending.action,
            "value": pending.value,
            "description": pending.description,
            "created_at": pending.created_at.isoformat(),
        }
    }


def _pending_from_setting_value(value: dict[str, Any] | None) -> PendingConfirmation | None:
    if not isinstance(value, dict) or not isinstance(value.get("value"), dict):
        return None
    payload = value["value"]
    try:
        code = payload["code"]
        action = payload["action"]
        pending_value = payload["value"]
        description = payload["description"]
        created_at = datetime.fromisoformat(payload["created_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(isinstance(item, str) and item for item in (code, action, pending_value, description)):
        return None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return PendingConfirmation(
        code=code,
        action=action,
        value=pending_value,
        description=description,
        created_at=created_at.astimezone(UTC),
    )


async def _save_runtime_setting(
    session_factory: async_sessionmaker,
    key: str,
    value: Any,
    updated_by_telegram_user_id: int | None,
) -> None:
    async with session_factory() as session:
        repo = SettingsRepository(session)
        await repo.set(key, {"value": value}, updated_by_telegram_user_id)
        await session.commit()


async def _save_runtime_settings(
    session_factory: async_sessionmaker,
    settings: Settings,
    values: dict[str, Any],
    updated_by_telegram_user_id: int | None,
) -> None:
    async with session_factory() as session:
        repo = SettingsRepository(session)
        for key, value in values.items():
            await repo.set(key, {"value": value}, updated_by_telegram_user_id)
        await session.commit()
    for key, value in values.items():
        setattr(settings, key, value)


def _slow_signals_preset_values() -> dict[str, Any]:
    return dict(SLOW_SIGNALS_PRESET)


async def _enable_paper_trading(
    session_factory: async_sessionmaker,
    settings: Settings,
    updated_by_telegram_user_id: int | None,
) -> None:
    async with session_factory() as session:
        repo = SettingsRepository(session)
        await repo.set("signal_only", {"value": False}, updated_by_telegram_user_id)
        await repo.set("paper_trading", {"value": True}, updated_by_telegram_user_id)
        await repo.set("trading_enabled", {"value": False}, updated_by_telegram_user_id)
        await repo.set("real_trading_admin_confirmed", {"value": False}, updated_by_telegram_user_id)
        await session.commit()
    settings.signal_only = False
    settings.paper_trading = True
    settings.trading_enabled = False
    settings.real_trading_admin_confirmed = False


async def _enable_demo_trading(
    session_factory: async_sessionmaker,
    settings: Settings,
    updated_by_telegram_user_id: int | None,
) -> None:
    async with session_factory() as session:
        repo = SettingsRepository(session)
        await repo.set("signal_only", {"value": False}, updated_by_telegram_user_id)
        await repo.set("paper_trading", {"value": False}, updated_by_telegram_user_id)
        await repo.set("trading_enabled", {"value": True}, updated_by_telegram_user_id)
        await repo.set("qt_account_mode", {"value": "demo"}, updated_by_telegram_user_id)
        await repo.set("real_trading_admin_confirmed", {"value": False}, updated_by_telegram_user_id)
        await session.commit()
    settings.signal_only = False
    settings.paper_trading = False
    settings.trading_enabled = True
    settings.qt_account_mode = "demo"
    settings.real_trading_admin_confirmed = False


async def _enable_real_trading(
    session_factory: async_sessionmaker,
    settings: Settings,
    updated_by_telegram_user_id: int | None,
) -> None:
    async with session_factory() as session:
        repo = SettingsRepository(session)
        await repo.set("signal_only", {"value": False}, updated_by_telegram_user_id)
        await repo.set("paper_trading", {"value": False}, updated_by_telegram_user_id)
        await repo.set("trading_enabled", {"value": True}, updated_by_telegram_user_id)
        await repo.set("qt_account_mode", {"value": "real"}, updated_by_telegram_user_id)
        await repo.set("real_trading_admin_confirmed", {"value": True}, updated_by_telegram_user_id)
        await session.commit()
    settings.signal_only = False
    settings.paper_trading = False
    settings.trading_enabled = True
    settings.qt_account_mode = "real"
    settings.real_trading_admin_confirmed = True


async def _set_int_runtime_setting(
    session_factory: async_sessionmaker,
    settings: Settings,
    message: Message,
    key: str,
    value: int,
    response: str,
) -> None:
    await _save_runtime_setting(session_factory, key, value, message.from_user.id if message.from_user else None)
    setattr(settings, key, value)
    await message.answer(response)


async def _set_decimal_runtime_setting(
    session_factory: async_sessionmaker,
    settings: Settings,
    message: Message,
    key: str,
    value: Decimal,
    response: str,
) -> None:
    await _save_runtime_setting(session_factory, key, str(value), message.from_user.id if message.from_user else None)
    setattr(settings, key, value)
    await message.answer(response)


async def _delete_message_quietly(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        return


async def _delete_recent_chat_messages(message: Message, previous_count: int) -> int:
    deleted = 0
    first_message_id = max(1, message.message_id - previous_count)
    for message_id in range(message.message_id, first_message_id - 1, -1):
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=message_id)
        except Exception:
            continue
        deleted += 1
        await asyncio.sleep(0.03)
    return deleted
