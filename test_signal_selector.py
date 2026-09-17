from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.config import Settings
from app.trading.signal_selector import SignalCandidate, SignalSelector, StaticSelectorStats
from app.trading.strategies.base import StrategySignal


@pytest.mark.asyncio
async def test_selector_rejects_all_in_signal_only_mode() -> None:
    selector = SignalSelector(Settings(_env_file=None))

    selection = await selector.select([_signal("EURUSD_otc", confidence=1)])

    assert selection.selected == []
    assert selection.rejected[0][1] == "signal_only_mode"


@pytest.mark.asyncio
async def test_selector_prioritizes_payout_then_confidence_then_performance() -> None:
    settings = _trading_settings(max_open_trades_total=1)
    lower_payout = SignalCandidate(_signal("EURUSD_otc", confidence=1), payout=Decimal("0.8"), strategy_performance=Decimal("0.9"))
    better_payout = SignalCandidate(_signal("GBPUSD_otc", confidence=0.1), payout=Decimal("0.9"), strategy_performance=Decimal("0.1"))
    selector = SignalSelector(settings, StaticSelectorStats([lower_payout, better_payout]))

    selection = await selector.select([lower_payout.signal, better_payout.signal])

    assert selection.selected == [better_payout]
    assert selection.rejected == [(lower_payout, "lower_priority")]


@pytest.mark.asyncio
async def test_selector_uses_confidence_when_payout_equal() -> None:
    settings = _trading_settings(max_open_trades_total=1)
    weak = SignalCandidate(_signal("EURUSD_otc", confidence=0.4), payout=Decimal("0.8"))
    strong = SignalCandidate(_signal("GBPUSD_otc", confidence=0.9), payout=Decimal("0.8"))
    selector = SignalSelector(settings, StaticSelectorStats([weak, strong]))

    selection = await selector.select([weak.signal, strong.signal])

    assert selection.selected == [strong]


@pytest.mark.asyncio
async def test_selector_rejects_open_trade_and_recent_loss() -> None:
    settings = _trading_settings()
    open_trade = SignalCandidate(_signal("EURUSD_otc"), open_trade_on_asset=True)
    recent_loss = SignalCandidate(_signal("GBPUSD_otc"), recent_loss_on_asset=True)
    selector = SignalSelector(settings, StaticSelectorStats([open_trade, recent_loss]))

    selection = await selector.select([open_trade.signal, recent_loss.signal])

    assert selection.selected == []
    assert selection.rejected == [(open_trade, "open_trade_on_asset"), (recent_loss, "recent_loss_on_asset")]


@pytest.mark.asyncio
async def test_selector_rejects_low_payout_and_open_limits() -> None:
    settings = _trading_settings(max_open_trades_total=1, max_open_trades_per_asset=1)
    low_payout = SignalCandidate(_signal("EURUSD_otc"), payout=Decimal("0.69"))
    open_limit = SignalCandidate(_signal("GBPUSD_otc"), open_trades_total=1)
    asset_limit = SignalCandidate(_signal("USDJPY_otc"), open_trades_for_asset=1)
    selector = SignalSelector(settings, StaticSelectorStats([low_payout, open_limit, asset_limit]))

    selection = await selector.select([low_payout.signal, open_limit.signal, asset_limit.signal])

    assert selection.selected == []
    assert selection.rejected == [
        (low_payout, "payout_below_minimum"),
        (open_limit, "max_open_trades_total"),
        (asset_limit, "max_open_trades_per_asset"),
    ]


def _trading_settings(**overrides):
    values = {
        "signal_only": False,
        "trading_enabled": True,
        "min_payout": Decimal("0.7"),
        "max_open_trades_total": 2,
        "max_open_trades_per_asset": 1,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _signal(asset: str, confidence: float | None = 0.5) -> StrategySignal:
    return StrategySignal(
        asset=asset,
        strategy_name="trend_pullback",
        direction="buy",
        confidence=confidence,
        reason={},
        candle_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        expiration_seconds=60,
    )
