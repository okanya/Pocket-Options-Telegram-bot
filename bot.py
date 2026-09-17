from __future__ import annotations

import logging
from typing import Protocol

from aiogram import Bot, Dispatcher
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.session import create_session_factory
from app.telegram.handlers import build_router
from app.telegram.session import create_telegram_bot
from app.trading.pocket_service import PocketConnectionService

logger = logging.getLogger(__name__)


class EngineStatusProvider(Protocol):
    def status(self):
        ...


async def run_bot(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    bot: Bot | None = None,
    engine_status_provider: EngineStatusProvider | None = None,
) -> None:
    if not settings.telegram_bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN is not configured; Telegram bot is not started")
        return

    session_factory = session_factory or create_session_factory(settings)
    pocket_service = PocketConnectionService(session_factory, account_type=settings.qt_account_mode)
    bot = bot or create_telegram_bot(settings.telegram_bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(settings, session_factory, pocket_service, engine_status_provider))

    logger.info("Starting Telegram polling")
    await dispatcher.start_polling(bot, handle_signals=False)
