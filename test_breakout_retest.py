from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.trading.candles import CandleData
from app.trading.strategies.base import StrategySettings
from app.trading.strategies.breakout_retest import BreakoutRetestStrategy


def test_breakout_retest_ignores_insufficient_candles() -> None:
    strategy = BreakoutRetestStrategy()

    signal = strategy.generate_signal(
        "EURUSD_otc",
        _base_candles(39),
        StrategySettings(expiration_seconds=60, timeframe_seconds=60),
    )

    assert signal is None


def test_breakout_retest_generates_buy_signal() -> None:
    strategy = BreakoutRetestStrategy()
    candles = _base_candles(38, high=99, low=96, close=98)
    candles[5] = _candle(5, open_price=98, high=100, low=97, close=99)
    candles[20] = _candle(20, open_price=98, high=100.01, low=97, close=99)
    candles.append(_candle(38, open_price=100.10, high=100.50, low=100.05, close=100.40))
    candles.append(_candle(39, open_price=100.08, high=100.30, low=99.95, close=100.12))

    signal = strategy.generate_signal(
        "EURUSD_otc",
        candles,
        StrategySettings(expiration_seconds=120, timeframe_seconds=60),
    )

    assert signal is not None
    assert signal.strategy_name == "breakout_retest"
    assert signal.direction == "buy"
    assert signal.expiration_seconds == 120
    assert signal.confidence == 1
    assert signal.reason["touches"] >= 2


def test_breakout_retest_generates_sell_signal() -> None:
    strategy = BreakoutRetestStrategy()
    candles = _base_candles(38, high=104, low=101, close=102)
    candles[4] = _candle(4, open_price=102, high=103, low=100, close=101)
    candles[18] = _candle(18, open_price=102, high=103, low=99.99, close=101)
    candles.append(_candle(38, open_price=99.90, high=99.95, low=99.50, close=99.60))
    candles.append(_candle(39, open_price=99.92, high=100.05, low=99.70, close=99.88))

    signal = strategy.generate_signal(
        "EURUSD_otc",
        candles,
        StrategySettings(expiration_seconds=60, timeframe_seconds=60),
    )

    assert signal is not None
    assert signal.direction == "sell"
    assert signal.confidence == 1


def test_breakout_retest_returns_none_when_breakout_candle_is_too_large() -> None:
    strategy = BreakoutRetestStrategy()
    candles = _base_candles(38, high=99, low=96, close=98)
    candles[5] = _candle(5, open_price=98, high=100, low=97, close=99)
    candles[20] = _candle(20, open_price=98, high=100.01, low=97, close=99)
    candles.append(_candle(38, open_price=100.10, high=103, low=100.05, close=102.00))
    candles.append(_candle(39, open_price=100.08, high=100.30, low=99.95, close=100.12))

    signal = strategy.generate_signal(
        "EURUSD_otc",
        candles,
        StrategySettings(expiration_seconds=60, timeframe_seconds=60),
    )

    assert signal is None


def test_breakout_retest_returns_none_for_weak_level() -> None:
    strategy = BreakoutRetestStrategy()
    candles = _base_candles(38, high=99, low=96, close=98)
    candles[5] = _candle(5, open_price=98, high=100, low=97, close=99)
    candles.append(_candle(38, open_price=100.10, high=100.50, low=100.05, close=100.40))
    candles.append(_candle(39, open_price=100.08, high=100.30, low=99.95, close=100.12))

    signal = strategy.generate_signal(
        "EURUSD_otc",
        candles,
        StrategySettings(expiration_seconds=60, timeframe_seconds=60),
    )

    assert signal is None


def _base_candles(count: int, high: float = 99, low: float = 96, close: float = 98) -> list[CandleData]:
    return [_candle(index, open_price=close, high=high, low=low, close=close) for index in range(count)]


def _candle(index: int, open_price: float, high: float, low: float, close: float) -> CandleData:
    return CandleData(
        asset="EURUSD_otc",
        timeframe_seconds=60,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index),
        open=Decimal(str(open_price)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
    )
