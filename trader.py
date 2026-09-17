from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from app.trading.strategies.base import StrategySignal


class BrokerClient(Protocol):
    async def buy(self, asset: str, amount: Decimal, expiration_seconds: int) -> tuple[str, dict[str, Any]]:
        ...

    async def sell(self, asset: str, amount: Decimal, expiration_seconds: int) -> tuple[str, dict[str, Any]]:
        ...

    async def check_win(self, trade_id: str, expiration_seconds: int | None = None) -> dict[str, Any] | None:
        ...


@dataclass(frozen=True)
class PaperTrade:
    mode: str
    asset: str
    strategy_name: str
    direction: str
    amount: Decimal
    expiration_seconds: int
    open_price: Decimal
    close_price: Decimal
    result: str
    profit: Decimal
    payout: Decimal
    status: str
    opened_at: datetime
    closed_at: datetime


@dataclass(frozen=True)
class OpenPaperTrade:
    mode: str
    asset: str
    strategy_name: str
    direction: str
    amount: Decimal
    expiration_seconds: int
    open_price: Decimal
    payout: Decimal
    status: str
    opened_at: datetime


@dataclass(frozen=True)
class OpenBrokerTrade:
    mode: str
    external_trade_id: str
    asset: str
    strategy_name: str
    direction: str
    amount: Decimal
    expiration_seconds: int
    open_price: Decimal | None
    payout: Decimal | None
    status: str
    opened_at: datetime
    raw_response: dict[str, Any]


@dataclass(frozen=True)
class BrokerTradeResult:
    mode: str
    external_trade_id: str
    asset: str
    strategy_name: str
    direction: str
    amount: Decimal
    expiration_seconds: int
    open_price: Decimal | None
    close_price: Decimal | None
    result: str
    profit: Decimal | None
    payout: Decimal | None
    status: str
    opened_at: datetime
    closed_at: datetime
    raw_response: dict[str, Any]


class PaperTrader:
    mode = "paper"

    def open(
        self,
        signal: StrategySignal,
        amount: Decimal,
        open_price: Decimal,
        payout: Decimal,
        opened_at: datetime,
    ) -> OpenPaperTrade:
        return OpenPaperTrade(
            mode=self.mode,
            asset=signal.asset,
            strategy_name=signal.strategy_name,
            direction=signal.direction,
            amount=amount,
            expiration_seconds=signal.expiration_seconds,
            open_price=open_price,
            payout=payout,
            status="opened",
            opened_at=opened_at,
        )

    def settle(
        self,
        signal: StrategySignal,
        amount: Decimal,
        open_price: Decimal,
        close_price: Decimal,
        payout: Decimal,
        opened_at: datetime,
        closed_at: datetime,
    ) -> PaperTrade:
        result = self._result(signal.direction, open_price, close_price)
        return PaperTrade(
            mode=self.mode,
            asset=signal.asset,
            strategy_name=signal.strategy_name,
            direction=signal.direction,
            amount=amount,
            expiration_seconds=signal.expiration_seconds,
            open_price=open_price,
            close_price=close_price,
            result=result,
            profit=self._profit(result, amount, payout),
            payout=payout,
            status="closed",
            opened_at=opened_at,
            closed_at=closed_at,
        )

    def _result(self, direction: str, open_price: Decimal, close_price: Decimal) -> str:
        if close_price == open_price:
            return "draw"
        if direction == "buy":
            return "win" if close_price > open_price else "loss"
        return "win" if close_price < open_price else "loss"

    def _profit(self, result: str, amount: Decimal, payout: Decimal) -> Decimal:
        if result == "win":
            return amount * payout
        if result == "loss":
            return -amount
        return Decimal("0")


class BrokerTrader:
    def __init__(self, mode: str) -> None:
        if mode not in {"demo", "real"}:
            raise ValueError("BrokerTrader mode must be demo or real")
        self.mode = mode

    async def open(
        self,
        client: BrokerClient,
        signal: StrategySignal,
        amount: Decimal,
        payout: Decimal | None,
        opened_at: datetime,
    ) -> OpenBrokerTrade:
        if signal.direction == "buy":
            external_trade_id, response = await client.buy(signal.asset, amount, signal.expiration_seconds)
        else:
            external_trade_id, response = await client.sell(signal.asset, amount, signal.expiration_seconds)

        return OpenBrokerTrade(
            mode=self.mode,
            external_trade_id=external_trade_id,
            asset=signal.asset,
            strategy_name=signal.strategy_name,
            direction=signal.direction,
            amount=amount,
            expiration_seconds=signal.expiration_seconds,
            open_price=_decimal_from_response(response, "open_price", "openPrice", "price"),
            payout=payout,
            status="opened",
            opened_at=opened_at,
            raw_response=response,
        )

    async def settle(
        self,
        client: BrokerClient,
        open_trade: OpenBrokerTrade,
        closed_at: datetime,
    ) -> BrokerTradeResult:
        response = await client.check_win(open_trade.external_trade_id, open_trade.expiration_seconds) or {}
        result = _result_from_response(response)
        return BrokerTradeResult(
            mode=self.mode,
            external_trade_id=open_trade.external_trade_id,
            asset=open_trade.asset,
            strategy_name=open_trade.strategy_name,
            direction=open_trade.direction,
            amount=open_trade.amount,
            expiration_seconds=open_trade.expiration_seconds,
            open_price=open_trade.open_price,
            close_price=_decimal_from_response(response, "close_price", "closePrice", "price"),
            result=result,
            profit=_decimal_from_response(response, "profit", "pnl"),
            payout=open_trade.payout,
            status="closed" if result in {"win", "loss", "draw"} else "failed",
            opened_at=open_trade.opened_at,
            closed_at=closed_at,
            raw_response=response,
        )


def _result_from_response(response: dict[str, Any]) -> str:
    result = response.get("result")
    if isinstance(result, str) and result.lower() in {"win", "loss", "draw"}:
        return result.lower()
    return "unknown"


def _decimal_from_response(response: dict[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        value = response.get(key)
        if value is None:
            continue
        try:
            return Decimal(str(value))
        except Exception:
            continue
    return None
