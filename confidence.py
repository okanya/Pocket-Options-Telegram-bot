from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Trade
from app.trading.strategies.base import StrategySignal

MIN_EXACT_SAMPLE = 20
MIN_STRATEGY_SAMPLE = 50
LOOKBACK_DAYS = 30


@dataclass(frozen=True)
class ConfidenceScore:
    value: float
    source: str
    sample_size: int
    win_rate: Decimal | None
    base_confidence: float | None


class HistoricalConfidenceScorer:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        lookback_days: int = LOOKBACK_DAYS,
        min_exact_sample: int = MIN_EXACT_SAMPLE,
        min_strategy_sample: int = MIN_STRATEGY_SAMPLE,
    ) -> None:
        self.session_factory = session_factory
        self.lookback_days = lookback_days
        self.min_exact_sample = min_exact_sample
        self.min_strategy_sample = min_strategy_sample

    async def score(self, signal: StrategySignal, now: datetime) -> ConfidenceScore:
        base_confidence = signal.confidence
        since = now.astimezone(UTC) - timedelta(days=self.lookback_days)
        hour = signal.candle_timestamp.astimezone(UTC).hour
        async with self.session_factory() as session:
            exact = await _trade_stats(
                session,
                since=since,
                asset=signal.asset,
                strategy_name=signal.strategy_name,
                expiration_seconds=signal.expiration_seconds,
                hour=hour,
            )
            if exact.sample_size >= self.min_exact_sample:
                return _score_from_stats(exact, "historical_exact", base_confidence)

            strategy = await _trade_stats(
                session,
                since=since,
                asset=signal.asset,
                strategy_name=signal.strategy_name,
                expiration_seconds=signal.expiration_seconds,
                hour=None,
            )
            if strategy.sample_size >= self.min_strategy_sample:
                return _score_from_stats(strategy, "historical_strategy", base_confidence)

        return ConfidenceScore(
            value=base_confidence if base_confidence is not None else 0.5,
            source="technical",
            sample_size=0,
            win_rate=None,
            base_confidence=base_confidence,
        )


@dataclass(frozen=True)
class _TradeStats:
    sample_size: int
    wins: int
    draws: int

    @property
    def win_rate(self) -> Decimal:
        if self.sample_size == 0:
            return Decimal("0")
        return (Decimal(self.wins) + Decimal("0.5") * Decimal(self.draws)) / Decimal(self.sample_size)


async def _trade_stats(
    session: AsyncSession,
    *,
    since: datetime,
    asset: str,
    strategy_name: str,
    expiration_seconds: int,
    hour: int | None,
) -> _TradeStats:
    filters = [
        Trade.status == "closed",
        Trade.result.in_(("win", "loss", "draw")),
        Trade.closed_at >= since,
        Trade.asset == asset,
        Trade.strategy_name == strategy_name,
        Trade.expiration_seconds == expiration_seconds,
    ]
    if hour is not None:
        filters.append(func.extract("hour", Trade.closed_at) == hour)
    row = (
        await session.execute(
            select(
                func.count(Trade.id),
                func.coalesce(func.sum(_result_value("win")), 0),
                func.coalesce(func.sum(_result_value("draw")), 0),
            ).where(*filters)
        )
    ).one()
    return _TradeStats(sample_size=int(row[0] or 0), wins=int(row[1] or 0), draws=int(row[2] or 0))


def _score_from_stats(stats: _TradeStats, source: str, base_confidence: float | None) -> ConfidenceScore:
    historical = stats.win_rate
    if base_confidence is None:
        value = historical
    else:
        value = (Decimal(str(base_confidence)) * Decimal("0.4")) + (historical * Decimal("0.6"))
    return ConfidenceScore(
        value=float(max(Decimal("0"), min(Decimal("1"), value))),
        source=source,
        sample_size=stats.sample_size,
        win_rate=historical,
        base_confidence=base_confidence,
    )


def _result_value(result: str):
    return case((Trade.result == result, 1), else_=0)
