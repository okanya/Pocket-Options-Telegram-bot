from __future__ import annotations

from aiogram import __version__
from aiohttp import ClientSession
from aiogram import Bot
from aiogram.client.session.aiohttp import SERVER_SOFTWARE, USER_AGENT, AiohttpSession


class EnvAwareAiohttpSession(AiohttpSession):
    async def create_session(self) -> ClientSession:
        if self._should_reset_connector:
            await self.close()

        if self._session is None or self._session.closed:
            self._session = ClientSession(
                connector=self._connector_type(**self._connector_init),
                headers={
                    USER_AGENT: f"{SERVER_SOFTWARE} aiogram/{__version__}",
                },
                trust_env=True,
            )
            self._should_reset_connector = False

        return self._session


def create_telegram_bot(token: str) -> Bot:
    return Bot(token=token, session=EnvAwareAiohttpSession())
