from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)


class PocketClientError(RuntimeError):
    """Base error for PocketOption wrapper failures."""


class PocketClientNotConnectedError(PocketClientError):
    """Raised when an operation requires an active PocketOption connection."""


class TradingOperationDisabledError(PocketClientError):
    """Raised while direct trading operations are intentionally disabled."""


class PocketClient:
    """PocketOption client facade.

    Market data and balance use the local Python Socket.IO implementation.
    Direct trading operations stay disabled until the raw trade protocol is implemented.
    """

    def __init__(self, ssid: str | None) -> None:
        self.ssid = ssid
        self._client: Any | None = None
        self._connected = False
        self._mocked = not bool(ssid)

    @property
    def mocked(self) -> bool:
        return self._mocked

    async def connect(self) -> None:
        if self._mocked:
            self._connected = True
            logger.warning("PocketOption SSID is not configured in DB; PocketClient runs in mocked mode")
            return

        from app.trading.raw_pocket_client import RawPocketClient

        self._client = RawPocketClient(self.ssid)
        await self._client.connect()
        self._connected = True
        logger.info("PocketOption connected")

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
        self._connected = False

    async def get_balance(self) -> Decimal | None:
        if self._mocked:
            return None
        client = self._require_client()
        return await client.get_balance()

    async def subscribe_candles(self, asset: str, timeframe_seconds: int) -> AsyncIterator[dict[str, Any]]:
        if self._mocked:
            return _empty_subscription()
        client = self._require_client()
        return client.subscribe_candles(asset, timeframe_seconds)

    async def buy(self, asset: str, amount: Decimal, expiration_seconds: int) -> tuple[str, dict[str, Any]]:
        if self._mocked:
            raise TradingOperationDisabledError("Trading is disabled in mocked PocketClient mode")
        client = self._require_client()
        return await client.buy(asset, amount, expiration_seconds)

    async def sell(self, asset: str, amount: Decimal, expiration_seconds: int) -> tuple[str, dict[str, Any]]:
        if self._mocked:
            raise TradingOperationDisabledError("Trading is disabled in mocked PocketClient mode")
        client = self._require_client()
        return await client.sell(asset, amount, expiration_seconds)

    async def check_win(self, trade_id: str, expiration_seconds: int | None = None) -> dict[str, Any] | None:
        if self._mocked:
            return None
        client = self._require_client()
        return await client.check_win(trade_id, expiration_seconds)

    async def get_payout(self, asset: str) -> Decimal | None:
        if self._mocked:
            return None
        client = self._require_client()
        return await client.get_payout(asset)

    async def get_payouts(self) -> dict[str, Decimal | None]:
        if self._mocked:
            return {}
        client = self._require_client()
        return await client.get_payouts()

    def is_connected(self) -> bool:
        return self._connected

    def _require_client(self) -> Any:
        if not self._connected or self._client is None:
            raise PocketClientNotConnectedError("PocketClient is not connected")
        return self._client


async def _empty_subscription() -> AsyncIterator[dict[str, Any]]:
    if False:
        yield {}


def _check_win_timeout(expiration_seconds: int | None) -> int:
    if expiration_seconds is None:
        return 60
    return max(60, expiration_seconds + 30)
