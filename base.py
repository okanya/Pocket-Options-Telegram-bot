from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel

from app.trading.candles import CandleData


class StrategySettings(BaseModel):
    expiration_seconds: int
    timeframe_seconds: int


class StrategySignal(BaseModel):
    asset: str
    strategy_name: str
    direction: Literal["buy", "sell"]
    confidence: float | None = None
    reason: dict
    candle_timestamp: datetime
    expiration_seconds: int


class BaseStrategy(Protocol):
    name: str
    min_candles: int

    def generate_signal(
        self,
        asset: str,
        candles: list[CandleData],
        settings: StrategySettings,
    ) -> StrategySignal | None:
        ...
