from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.repositories import SettingsRepository
from app.trading.strategies.base import StrategySignal
from app.trading.trader import BrokerTradeResult, OpenBrokerTrade, OpenPaperTrade, PaperTrade

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradeMessageRef:
    chat_id: int
    message_id: int
    text: str


class TradeMessageStore(Protocol):
    async def save(self, external_trade_id: str, ref: TradeMessageRef) -> None:
        ...

    async def get(self, external_trade_id: str) -> TradeMessageRef | None:
        ...

    async def delete(self, external_trade_id: str) -> None:
        ...

    async def mark_closed(self, external_trade_id: str) -> None:
        ...

    async def is_closed(self, external_trade_id: str) -> bool:
        ...


class DatabaseTradeMessageStore:
    KEY_PREFIX = "telegram_trade_message:"
    CLOSED_KEY_PREFIX = "telegram_trade_closed:"

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def save(self, external_trade_id: str, ref: TradeMessageRef) -> None:
        async with self.session_factory() as session:
            await SettingsRepository(session).set(
                self._key(external_trade_id),
                {"chat_id": ref.chat_id, "message_id": ref.message_id, "text": ref.text},
            )
            await session.commit()

    async def get(self, external_trade_id: str) -> TradeMessageRef | None:
        async with self.session_factory() as session:
            setting = await SettingsRepository(session).get(self._key(external_trade_id))
        if setting is None:
            return None
        try:
            chat_id = int(setting.value["chat_id"])
            message_id = int(setting.value["message_id"])
            text = str(setting.value["text"])
        except (KeyError, TypeError, ValueError):
            return None
        return TradeMessageRef(chat_id=chat_id, message_id=message_id, text=text)

    async def delete(self, external_trade_id: str) -> None:
        async with self.session_factory() as session:
            await SettingsRepository(session).delete(self._key(external_trade_id))
            await session.commit()

    async def mark_closed(self, external_trade_id: str) -> None:
        async with self.session_factory() as session:
            await SettingsRepository(session).set(self._closed_key(external_trade_id), {"value": True})
            await session.commit()

    async def is_closed(self, external_trade_id: str) -> bool:
        async with self.session_factory() as session:
            setting = await SettingsRepository(session).get(self._closed_key(external_trade_id))
        if setting is None:
            return False
        return bool(setting.value.get("value")) if isinstance(setting.value, dict) else False

    def _key(self, external_trade_id: str) -> str:
        return f"{self.KEY_PREFIX}{external_trade_id}"

    def _closed_key(self, external_trade_id: str) -> str:
        return f"{self.CLOSED_KEY_PREFIX}{external_trade_id}"


class TelegramNotifier:
    def __init__(
        self,
        bot: Bot,
        settings: Settings,
        *,
        min_send_interval_seconds: float = 0.0,
        trade_message_store: TradeMessageStore | None = None,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.min_send_interval_seconds = max(min_send_interval_seconds, 0.0)
        self.trade_message_store = trade_message_store
        self._send_lock = asyncio.Lock()
        self._last_sent_at = 0.0
        self._broker_trade_messages: dict[str, TradeMessageRef] = {}
        self._closed_broker_trade_notifications: set[str] = set()

    async def send(self, text: str) -> Message | None:
        if self.settings.telegram_notify_chat_id is None:
            logger.debug("Telegram notify chat is not configured")
            return None
        async with self._send_lock:
            await self._wait_for_rate_limit()
            try:
                message = await self.bot.send_message(chat_id=self.settings.telegram_notify_chat_id, text=text)
                self._last_sent_at = time.monotonic()
                return message
            except TelegramRetryAfter as exc:
                retry_after = float(getattr(exc, "retry_after", 1))
                logger.warning("Telegram notification rate limited; retrying in %ss", retry_after)
                await asyncio.sleep(retry_after)
                try:
                    message = await self.bot.send_message(chat_id=self.settings.telegram_notify_chat_id, text=text)
                    self._last_sent_at = time.monotonic()
                    return message
                except Exception as retry_exc:
                    logger.warning("Telegram notification could not be sent after retry: %s", retry_exc)
            except Exception as exc:
                logger.warning("Telegram notification could not be sent: %s", exc)
        return None

    async def _wait_for_rate_limit(self) -> None:
        if self.min_send_interval_seconds <= 0:
            return
        elapsed = time.monotonic() - self._last_sent_at
        delay = self.min_send_interval_seconds - elapsed
        if delay > 0:
            await asyncio.sleep(delay)

    async def bot_started(self, mode: str) -> None:
        await self.send(
            "Bot started\n"
            f"Mode: {mode}\n"
            f"Assets: {', '.join(self.settings.active_assets)}\n"
            f"Strategy: {self.settings.active_strategy}"
        )

    async def bot_stopped(self) -> None:
        await self.send("Bot stopped")

    async def pocket_connected(self) -> None:
        await self.send("PocketOption connected")

    async def pocket_connection_error(self, error: str) -> None:
        await self.send(
            "PocketOption connection error\n"
            f"Error: {error}"
        )

    async def reconnect_attempt(self, attempt: int, delay_seconds: float) -> None:
        await self.send(
            "Reconnect attempt\n"
            f"Attempt: {attempt}\n"
            f"Delay: {delay_seconds:g}s"
        )

    async def new_signal(self, signal: StrategySignal) -> None:
        logger.info(
            "New signal asset=%s strategy=%s direction=%s confidence=%s expiration=%ss",
            signal.asset,
            signal.strategy_name,
            signal.direction,
            signal.confidence if signal.confidence is not None else "unknown",
            signal.expiration_seconds,
        )
        await self.send(_format_signal(signal))

    async def signal_selected(self, signal: StrategySignal) -> None:
        logger.info(
            "Selected signal asset=%s strategy=%s direction=%s",
            signal.asset,
            signal.strategy_name,
            signal.direction,
        )

    async def signal_rejected(self, signal: StrategySignal, reason: str) -> None:
        logger.info(
            "Rejected signal asset=%s strategy=%s direction=%s reason=%s",
            signal.asset,
            signal.strategy_name,
            signal.direction,
            reason,
        )

    async def paper_trade_opened(self, trade: OpenPaperTrade) -> None:
        await self.send(
            "Paper trade opened\n"
            f"Asset: {trade.asset}\n"
            f"Strategy: {trade.strategy_name}\n"
            f"Direction: {trade.direction}\n"
            f"Amount: {trade.amount}\n"
            f"Open price: {trade.open_price}\n"
            f"Expiration: {trade.expiration_seconds}s"
        )

    async def paper_trade_closed(self, trade: PaperTrade) -> None:
        await self.send(
            "Paper trade closed\n"
            f"Asset: {trade.asset}\n"
            f"Strategy: {trade.strategy_name}\n"
            f"Direction: {trade.direction}\n"
            f"Result: {trade.result}\n"
            f"Profit: {trade.profit}\n"
            f"Open price: {trade.open_price}\n"
            f"Close price: {trade.close_price}"
        )

    async def trade_opened(self, trade: OpenBrokerTrade) -> None:
        text = _format_broker_trade_opened(trade)
        message = await self.send(text)
        if message is not None and trade.external_trade_id:
            ref = TradeMessageRef(chat_id=message.chat.id, message_id=message.message_id, text=text)
            self._broker_trade_messages[trade.external_trade_id] = ref
            if self.trade_message_store is not None:
                try:
                    await self.trade_message_store.save(trade.external_trade_id, ref)
                except Exception as exc:
                    logger.warning("Telegram trade message ref could not be saved: %s", exc)

    async def trade_closed(self, trade: BrokerTradeResult) -> None:
        if await self._is_trade_close_notified(trade.external_trade_id):
            logger.info("Skipping duplicate trade closed notification external_trade_id=%s", trade.external_trade_id)
            return

        close_text = _format_broker_trade_result(trade)
        opened_message = await self._pop_trade_message_ref(trade.external_trade_id)
        if opened_message is not None:
            try:
                await self.bot.edit_message_text(
                    chat_id=opened_message.chat_id,
                    message_id=opened_message.message_id,
                    text=f"{opened_message.text}\n\n{close_text}",
                )
                await self._mark_trade_close_notified(trade.external_trade_id)
                return
            except Exception as exc:
                logger.warning("Telegram trade message could not be edited: %s", exc)
        message = await self.send(
            "Trade closed\n"
            f"External ID: {trade.external_trade_id}\n"
            f"{close_text}"
        )
        if message is not None:
            await self._mark_trade_close_notified(trade.external_trade_id)

    async def _pop_trade_message_ref(self, external_trade_id: str) -> TradeMessageRef | None:
        ref = self._broker_trade_messages.pop(external_trade_id, None)
        if ref is None and self.trade_message_store is not None:
            try:
                ref = await self.trade_message_store.get(external_trade_id)
            except Exception as exc:
                logger.warning("Telegram trade message ref could not be loaded: %s", exc)
        if ref is not None and self.trade_message_store is not None:
            try:
                await self.trade_message_store.delete(external_trade_id)
            except Exception as exc:
                logger.warning("Telegram trade message ref could not be deleted: %s", exc)
        return ref

    async def _is_trade_close_notified(self, external_trade_id: str) -> bool:
        if external_trade_id in self._closed_broker_trade_notifications:
            return True
        if self.trade_message_store is None:
            return False
        try:
            is_closed = await self.trade_message_store.is_closed(external_trade_id)
        except Exception as exc:
            logger.warning("Telegram trade closed marker could not be loaded: %s", exc)
            return False
        if is_closed:
            self._closed_broker_trade_notifications.add(external_trade_id)
        return is_closed

    async def _mark_trade_close_notified(self, external_trade_id: str) -> None:
        self._closed_broker_trade_notifications.add(external_trade_id)
        if self.trade_message_store is None:
            return
        try:
            await self.trade_message_store.mark_closed(external_trade_id)
        except Exception as exc:
            logger.warning("Telegram trade closed marker could not be saved: %s", exc)


def _format_broker_trade_opened(trade: OpenBrokerTrade) -> str:
    return (
        "🟢 Opened\n"
        f"📊 {trade.asset} | {trade.strategy_name}\n"
        f"{_direction_icon(trade.direction)} {_direction_label(trade.direction)} | {_mode_icon(trade.mode)} {trade.mode}\n"
        f"💵 Amount: {trade.amount}\n"
        f"⏱ Expiration: {trade.expiration_seconds}s\n"
        f"🆔 {trade.external_trade_id}"
    )


def _format_signal(signal: StrategySignal) -> str:
    confidence = f"{signal.confidence:.2f}" if signal.confidence is not None else "unknown"
    return (
        "🔔 Signal\n"
        f"📊 {signal.asset} | {signal.strategy_name}\n"
        f"{_direction_icon(signal.direction)} {_direction_label(signal.direction)}\n"
        f"🎯 Confidence: {confidence}\n"
        f"⏱ Expiration: {signal.expiration_seconds}s"
    )


def _format_broker_trade_result(trade: BrokerTradeResult) -> str:
    return (
        f"{_result_icon(trade.result)} {trade.result.upper()}\n"
        f"{_profit_icon(trade.profit)} Profit: {_format_profit(trade.profit)}"
    )


def _direction_icon(direction: str) -> str:
    return "⬆️" if direction == "buy" else "⬇️"


def _direction_label(direction: str) -> str:
    return direction.upper()


def _mode_icon(mode: str) -> str:
    if mode == "real":
        return "🚨"
    if mode == "paper":
        return "📝"
    return "🧪"


def _result_icon(result: str) -> str:
    if result == "win":
        return "✅"
    if result == "loss":
        return "🔴"
    if result == "draw":
        return "⚪"
    return "❔"


def _profit_icon(profit) -> str:
    if profit is None:
        return "❔"
    if profit > 0:
        return "💰"
    if profit < 0:
        return "💸"
    return "➖"


def _format_profit(profit) -> str:
    if profit is None:
        return "unknown"
    return f"+{profit}" if profit > 0 else str(profit)
