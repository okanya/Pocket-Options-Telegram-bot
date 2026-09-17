from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import Settings
from app.trading.risk import RiskContext, RiskManager, RiskSnapshot, StaticRiskStats, TradeSnapshot
from app.trading.strategies.base import StrategySignal


@pytest.mark.asyncio
async def test_default_mode_rejects_trading() -> None:
    decision = await RiskManager(Settings(_env_file=None)).evaluate(_context())

    assert decision.allowed is False
    assert decision.reason == "signal_only_enabled"


@pytest.mark.asyncio
async def test_signal_only_rejects_even_when_trading_enabled() -> None:
    settings = Settings(_env_file=None, trading_enabled=True, signal_only=True)

    decision = await RiskManager(settings).evaluate(_context(admin_confirmed_trading=True))

    assert decision.allowed is False
    assert decision.reason == "signal_only_enabled"


@pytest.mark.asyncio
async def test_admin_confirmation_required() -> None:
    settings = _trading_settings()

    decision = await RiskManager(settings).evaluate(_context(admin_confirmed_trading=False))

    assert decision.allowed is False
    assert decision.reason == "admin_confirmation_required"


@pytest.mark.asyncio
async def test_paper_trading_does_not_require_trading_enabled_or_admin_confirmation() -> None:
    settings = Settings(_env_file=None, signal_only=False, paper_trading=True, trading_enabled=False)

    decision = await RiskManager(settings).evaluate(_context(admin_confirmed_trading=False))

    assert decision.allowed is True


@pytest.mark.asyncio
async def test_data_unhealthy_rejects_trade() -> None:
    settings = _trading_settings()

    decision = await RiskManager(settings).evaluate(_context(admin_confirmed_trading=True, data_healthy=False))

    assert decision.allowed is False
    assert decision.reason == "data_unhealthy"


@pytest.mark.asyncio
async def test_daily_stop_loss_rejects_trade() -> None:
    settings = _trading_settings()
    stats = StaticRiskStats(RiskSnapshot(daily_pnl=Decimal("-10")))

    decision = await RiskManager(settings, stats).evaluate(_context(admin_confirmed_trading=True))

    assert decision.allowed is False
    assert decision.reason == "daily_stop_loss"


@pytest.mark.asyncio
async def test_max_consecutive_losses_rejects_trade() -> None:
    settings = _trading_settings()
    stats = StaticRiskStats(RiskSnapshot(consecutive_losses=2))

    decision = await RiskManager(settings, stats).evaluate(_context(admin_confirmed_trading=True))

    assert decision.allowed is False
    assert decision.reason == "max_consecutive_losses"


@pytest.mark.asyncio
async def test_payout_below_minimum_rejects_trade() -> None:
    settings = _trading_settings()

    decision = await RiskManager(settings).evaluate(
        _context(admin_confirmed_trading=True, payout=Decimal("0.69")),
    )

    assert decision.allowed is False
    assert decision.reason == "payout_below_minimum"


@pytest.mark.asyncio
async def test_loss_cooldown_rejects_trade() -> None:
    settings = _trading_settings(loss_cooldown_seconds=900)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    stats = StaticRiskStats(
        RiskSnapshot(
            last_loss=TradeSnapshot(
                asset="EURUSD_otc",
                result="loss",
                opened_at=now - timedelta(minutes=2),
                closed_at=now - timedelta(minutes=1),
            )
        )
    )

    decision = await RiskManager(settings, stats).evaluate(_context(admin_confirmed_trading=True, now=now))

    assert decision.allowed is False
    assert decision.reason == "loss_cooldown"


@pytest.mark.asyncio
async def test_same_candle_rejects_when_enabled() -> None:
    settings = _trading_settings(one_trade_per_candle=True)
    stats = StaticRiskStats(RiskSnapshot(same_candle_trades=1))

    decision = await RiskManager(settings, stats).evaluate(_context(admin_confirmed_trading=True))

    assert decision.allowed is False
    assert decision.reason == "one_trade_per_candle"


@pytest.mark.asyncio
async def test_happy_path_allows_trade() -> None:
    settings = _trading_settings()

    decision = await RiskManager(settings).evaluate(_context(admin_confirmed_trading=True))

    assert decision.allowed is True
    assert decision.reason is None


def _trading_settings(**overrides):
    return Settings(
        _env_file=None,
        trading_enabled=True,
        signal_only=False,
        max_trades_total_per_day=5,
        max_trades_per_asset_per_day=2,
        max_open_trades_total=1,
        max_open_trades_per_asset=1,
        max_losses_per_day=2,
        max_consecutive_losses=2,
        daily_stop_loss=Decimal("10"),
        daily_take_profit=Decimal("20"),
        min_payout=Decimal("0.7"),
        **overrides,
    )


def _context(
    admin_confirmed_trading: bool = False,
    data_healthy: bool = True,
    payout: Decimal | None = Decimal("0.8"),
    now: datetime | None = None,
) -> RiskContext:
    now = now or datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    return RiskContext(
        signal=StrategySignal(
            asset="EURUSD_otc",
            strategy_name="trend_pullback",
            direction="buy",
            confidence=1,
            reason={},
            candle_timestamp=now,
            expiration_seconds=60,
        ),
        amount=Decimal("1"),
        payout=payout,
        data_healthy=data_healthy,
        now=now,
        admin_confirmed_trading=admin_confirmed_trading,
    )
