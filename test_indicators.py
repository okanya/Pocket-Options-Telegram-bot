from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.trading.candles import CandleData
from app.trading.indicators import (
    adx,
    bollinger_bands,
    candle_body_size,
    ema,
    find_local_resistance,
    find_local_support,
    lower_wick_ratio,
    rsi,
    upper_wick_ratio,
)


def test_ema_returns_none_until_period_and_then_smooths() -> None:
    values = [1, 2, 3, 4, 5]

    result = ema(values, period=3)

    assert result[:2] == [None, None]
    assert result[2:] == [2, 3, 4]


def test_rsi_extremes() -> None:
    rising = rsi([1, 2, 3, 4, 5], period=2)
    falling = rsi([5, 4, 3, 2, 1], period=2)

    assert rising[-1] == 100
    assert falling[-1] == 0


def test_bollinger_bands() -> None:
    bands = bollinger_bands([1, 2, 3], period=3, stddev=2)

    assert bands.middle == [None, None, 2]
    assert round(bands.upper[-1], 6) == 3.632993
    assert round(bands.lower[-1], 6) == 0.367007


def test_adx_produces_values_after_warmup() -> None:
    candles = [_candle(index, high=10 + index, low=9 + index, close=9.5 + index) for index in range(35)]

    result = adx(candles, period=14)

    assert result.adx[-1] is not None
    assert result.plus_di[-1] is not None
    assert result.minus_di[-1] is not None


def test_candle_shape_helpers() -> None:
    candle = _candle(0, open_price=10, high=12, low=8, close=11)

    assert candle_body_size(candle) == 1
    assert upper_wick_ratio(candle) == 0.25
    assert lower_wick_ratio(candle) == 0.5


def test_support_resistance_helpers() -> None:
    candles = [
        _candle(0, high=10, low=5, close=7),
        _candle(1, high=10.001, low=5.001, close=7),
        _candle(2, high=9, low=6, close=7),
    ]

    resistance = find_local_resistance(candles, lookback=3, tolerance_ratio=0.001)
    support = find_local_support(candles, lookback=3, tolerance_ratio=0.001)

    assert resistance is not None
    assert resistance.touches == 2
    assert support is not None
    assert support.touches == 2


def _candle(
    index: int,
    high: float,
    low: float,
    close: float,
    open_price: float | None = None,
) -> CandleData:
    return CandleData(
        asset="EURUSD_otc",
        timeframe_seconds=60,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index),
        open=Decimal(str(open_price if open_price is not None else close)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
    )
