from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories import BalanceSnapshotRepository, BotEventRepository, SecretSettingsRepository
from app.trading.pocket_client import PocketClient, PocketClientError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PocketConnectionStatus:
    ssid_configured: bool
    connected: bool
    mocked: bool
    error: str | None = None


@dataclass(frozen=True)
class BalanceResult:
    balance: Decimal | None
    status: PocketConnectionStatus


@dataclass(frozen=True)
class AssetsResult:
    payouts: dict[str, Decimal | None]
    status: PocketConnectionStatus


class ConnectedPocketClientProvider(Protocol):
    def current_client(self) -> Any | None:
        ...


class PocketConnectionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        client_factory: type[PocketClient] = PocketClient,
        connected_client_provider: ConnectedPocketClientProvider | None = None,
        account_type: str = "unknown",
    ) -> None:
        self.session_factory = session_factory
        self.client_factory = client_factory
        self.connected_client_provider = connected_client_provider
        self.account_type = account_type

    async def status(self) -> PocketConnectionStatus:
        ssid = await self._load_ssid()
        if not ssid:
            return PocketConnectionStatus(ssid_configured=False, connected=False, mocked=True)
        return PocketConnectionStatus(ssid_configured=True, connected=False, mocked=False)

    async def get_balance(self, source: str = "manual") -> BalanceResult:
        ssid = await self._load_ssid()
        if not ssid:
            status = PocketConnectionStatus(ssid_configured=False, connected=False, mocked=True)
            return BalanceResult(balance=None, status=status)

        connected_client = self._current_connected_client(ssid)
        if connected_client is not None:
            try:
                balance = await connected_client.get_balance()
                await self._record_balance(balance, source)
                return BalanceResult(
                    balance=balance,
                    status=PocketConnectionStatus(
                        ssid_configured=True,
                        connected=connected_client.is_connected(),
                        mocked=getattr(connected_client, "mocked", False),
                    ),
                )
            except Exception as exc:
                logger.debug("PocketOption balance request through active client failed: %s", _sanitize_error(exc, ssid))

        client = self.client_factory(ssid=ssid)
        try:
            await client.connect()
            balance = await client.get_balance()
            await self._record_balance(balance, source)
            return BalanceResult(
                balance=balance,
                status=PocketConnectionStatus(
                    ssid_configured=True,
                    connected=client.is_connected(),
                    mocked=client.mocked,
                ),
            )
        except Exception as exc:
            error = _sanitize_error(exc, ssid)
            logger.warning("PocketOption balance request failed: %s", error)
            await self._record_event("error", "pocket_option_balance_error", "PocketOption balance request failed", error)
            return BalanceResult(
                balance=None,
                status=PocketConnectionStatus(
                    ssid_configured=True,
                    connected=False,
                    mocked=client.mocked,
                    error=error,
                ),
            )
        finally:
            try:
                await client.disconnect()
            except PocketClientError as exc:
                logger.debug("PocketOption disconnect failed: %s", _sanitize_error(exc, ssid))

    async def get_assets(self) -> AssetsResult:
        ssid = await self._load_ssid()
        if not ssid:
            status = PocketConnectionStatus(ssid_configured=False, connected=False, mocked=True)
            return AssetsResult(payouts={}, status=status)

        client = self.client_factory(ssid=ssid)
        try:
            await client.connect()
            payouts = await client.get_payouts()
            return AssetsResult(
                payouts=payouts,
                status=PocketConnectionStatus(
                    ssid_configured=True,
                    connected=client.is_connected(),
                    mocked=client.mocked,
                ),
            )
        except Exception as exc:
            error = _sanitize_error(exc, ssid)
            logger.warning("PocketOption assets request failed: %s", error)
            await self._record_event("error", "pocket_option_assets_error", "PocketOption assets request failed", error)
            return AssetsResult(
                payouts={},
                status=PocketConnectionStatus(
                    ssid_configured=True,
                    connected=False,
                    mocked=client.mocked,
                    error=error,
                ),
            )
        finally:
            try:
                await client.disconnect()
            except PocketClientError as exc:
                logger.debug("PocketOption disconnect failed: %s", _sanitize_error(exc, ssid))

    async def _load_ssid(self) -> str | None:
        async with self.session_factory() as session:
            return await SecretSettingsRepository(session).get_pocket_option_ssid()

    def _current_connected_client(self, ssid: str) -> Any | None:
        if self.connected_client_provider is None:
            return None
        current_client = getattr(self.connected_client_provider, "current_client", None)
        if not callable(current_client):
            return None
        client = current_client()
        if client is None:
            return None
        if getattr(client, "ssid", ssid) != ssid:
            return None
        try:
            if not client.is_connected():
                return None
        except Exception:
            return None
        return client

    async def _record_event(self, level: str, event_type: str, message: str, error: str) -> None:
        async with self.session_factory() as session:
            repo = BotEventRepository(session)
            await repo.create(level=level, event_type=event_type, message=message, payload={"error": error})
            await session.commit()

    async def _record_balance(self, balance: Decimal | None, source: str) -> None:
        if balance is None:
            return
        try:
            async with self.session_factory() as session:
                await BalanceSnapshotRepository(session).create(
                    account_type=self.account_type,
                    balance=balance,
                    source=source,
                )
                await session.commit()
        except Exception as exc:
            logger.warning("PocketOption balance snapshot could not be saved: %s", exc)


def _sanitize_error(exc: Exception, secret: str | None) -> str:
    message = str(exc) or exc.__class__.__name__
    return message.replace(secret, "***") if secret else message
