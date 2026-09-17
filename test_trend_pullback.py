from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.trading.candles import CandleData
from app.trading.strategies.base import StrategySettings
from app.trading.strategies.trend_pullback import TrendPullbackStrategy


def test_trend_pullback_ignores_insufficient_candles() -> None:
    strategy = TrendPullbackStrategy()

    signal = strategy.generate_signal("EURUSD_otc", _candles(59), StrategySettings(expiration_seconds=60, timeframe_seconds=60))

    assert signal is None


def test_trend_pullback_generates_buy_signal(monkeypatch) -> None:
    strategy = TrendPullbackStrategy()
    candles = _candles(60, close=100)

    monkeypatch.setattr("app.trading.strategies.trend_pullback.ema", _ema_for_buy)
    monkeypatch.setattr("app.trading.strategies.trend_pullback.rsi", lambda values, period: _series(60, 45, 47))

    signal = strategy.generate_signal("EURUSD_otc", candles, StrategySettings(expiration_seconds=120, timeframe_seconds=60))

    assert signal is not None
    assert signal.direction == "buy"
    assert signal.strategy_name == "trend_pullback"
    assert signal.expiration_seconds == 120
    assert signal.confidence == 1


def test_trend_pullback_generates_sell_signal(monkeypatch) -> None:
    strategy = TrendPullbackStrategy()
    candles = _candles(60, close=100)

    monkeypatch.setattr("app.trading.strategies.trend_pullback.ema", _ema_for_sell)
    monkeypatch.setattr("app.trading.strategies.trend_pullback.rsi", lambda values, period: _series(60, 55, 53))

    signal = strategy.generate_signal("EURUSD_otc", candles, StrategySettings(expiration_seconds=60, timeframe_seconds=60))

    assert signal is not None
    assert signal.direction == "sell"
    assert signal.confidence == 1


def test_trend_pullback_returns_none_when_conditions_fail(monkeypatch) -> None:
    strategy = TrendPullbackStrategy()
    candles = _candles(60, close=100)

    monkeypatch.setattr("app.trading.strategies.trend_pullback.ema", _ema_for_buy)
    monkeypatch.setattr("app.trading.strategies.trend_pullback.rsi", lambda values, period: _series(60, 70, 72))

    signal = strategy.generate_signal("EURUSD_otc", candles, StrategySettings(expiration_seconds=60, timeframe_seconds=60))

    assert signal is None


def _candles(count: int, close: float = 100) -> list[CandleData]:
    return [
        CandleData(
            asset="EURUSD_otc",
            timeframe_seconds=60,
            timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index),
            open=Decimal(str(close)),
            high=Decimal(str(close + 1)),
            low=Decimal(str(close - 1)),
            close=Decimal(str(close)),
        )
        for index in range(count)
    ]


def _series(count: int, previous: float, latest: float) -> list[float | None]:
    values: list[float | None] = [None] * count
    values[-2] = previous
    values[-1] = latest
    return values


def _ema_for_buy(values, period):
    if period == 9:
        return _series(len(values), 91, 92)
    if period == 21:
        return _series(len(values), 89, 90)
    return _series(len(values), 94, 95)


def _ema_for_sell(values, period):
    if period == 9:
        return _series(len(values), 109, 108)
    if period == 21:
        return _series(len(values), 111, 110)
    return _series(len(values), 106, 105)
