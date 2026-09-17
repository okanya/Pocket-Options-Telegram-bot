from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from app.config import Settings
from app.trading.strategies.base import StrategySignal


@dataclass(frozen=True)
class SignalCandidate:
    signal: StrategySignal
    payout: Decimal | None = None
    strategy_performance: Decimal | None = None
    recent_loss_on_asset: bool = False
    open_trade_on_asset: bool = False
    open_trades_total: int = 0
    open_trades_for_asset: int = 0
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SignalSelection:
    selected: list[SignalCandidate]
    rejected: list[tuple[SignalCandidate, str]]


class SelectorStats(Protocol):
    async def enrich(self, signals: list[StrategySignal]) -> list[SignalCandidate]:
        ...


class StaticSelectorStats:
    def __init__(self, candidates: list[SignalCandidate] | None = None) -> None:
        self.candidates = candidates

    async def enrich(self, signals: list[StrategySignal]) -> list[SignalCandidate]:
        if self.candidates is not None:
            return self.candidates
        return [SignalCandidate(signal=signal) for signal in signals]


class SignalSelector:
    def __init__(self, settings: Settings, stats: SelectorStats | None = None) -> None:
        self.settings = settings
        self.stats = stats or StaticSelectorStats()

    async def select(self, signals: list[StrategySignal]) -> SignalSelection:
        candidates = await self.stats.enrich(signals)
        rejected: list[tuple[SignalCandidate, str]] = []

        if self.settings.signal_only:
            return SignalSelection(selected=[], rejected=[(candidate, "signal_only_mode") for candidate in candidates])

        eligible: list[SignalCandidate] = []
        for candidate in candidates:
            reason = self._rejection_reason(candidate)
            if reason:
                rejected.append((candidate, reason))
            else:
                eligible.append(candidate)

        eligible.sort(key=_score, reverse=True)
        available_slots = max(self.settings.max_open_trades_total - _max_open_total(eligible), 0)
        if available_slots <= 0:
            return SignalSelection(
                selected=[],
                rejected=rejected + [(candidate, "max_open_trades_total") for candidate in eligible],
            )

        selected = eligible[:available_slots]
        rejected.extend((candidate, "lower_priority") for candidate in eligible[available_slots:])
        return SignalSelection(selected=selected, rejected=rejected)

    def _rejection_reason(self, candidate: SignalCandidate) -> str | None:
        if candidate.open_trades_total >= self.settings.max_open_trades_total:
            return "max_open_trades_total"
        if candidate.open_trades_for_asset >= self.settings.max_open_trades_per_asset:
            return "max_open_trades_per_asset"
        if candidate.open_trade_on_asset:
            return "open_trade_on_asset"
        if candidate.recent_loss_on_asset:
            return "recent_loss_on_asset"
        if candidate.payout is not None and candidate.payout < self.settings.min_payout:
            return "payout_below_minimum"
        return None


def _score(candidate: SignalCandidate) -> tuple[Decimal, Decimal, Decimal]:
    payout_score = candidate.payout if candidate.payout is not None else Decimal("-1")
    confidence_score = Decimal(str(candidate.signal.confidence)) if candidate.signal.confidence is not None else Decimal("0")
    performance_score = candidate.strategy_performance if candidate.strategy_performance is not None else Decimal("0")
    return payout_score, confidence_score, performance_score


def _max_open_total(candidates: list[SignalCandidate]) -> int:
    return max((candidate.open_trades_total for candidate in candidates), default=0)
