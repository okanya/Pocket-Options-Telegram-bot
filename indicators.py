from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from statistics import mean
from typing import NamedTuple

from app.trading.candles import CandleData


class BollingerBands(NamedTuple):
    middle: list[float | None]
    upper: list[float | None]
    lower: list[float | None]


class ADXResult(NamedTuple):
    adx: list[float | None]
    plus_di: list[float | None]
    minus_di: list[float | None]


@dataclass(frozen=True)
class PriceLevel:
    price: float
    touches: int
    index: int


def ema(values: list[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    if not values:
        return []

    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result

    multiplier = 2 / (period + 1)
    current = mean(values[:period])
    result[period - 1] = current
    for index in range(period, len(values)):
        current = (values[index] - current) * multiplier + current
        result[index] = current
    return result


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    if period <= 0:
        raise ValueError("period must be positive")
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result

    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = mean(gains)
    avg_loss = mean(losses)
    result[period] = _rsi_value(avg_gain, avg_loss)

    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        gain = max(change, 0)
        loss = max(-change, 0)
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        result[index] = _rsi_value(avg_gain, avg_loss)

    return result


def bollinger_bands(values: list[float], period: int = 20, stddev: float = 2.0) -> BollingerBands:
    if period <= 0:
        raise ValueError("period must be positive")
    middle: list[float | None] = [None] * len(values)
    upper: list[float | None] = [None] * len(values)
    lower: list[float | None] = [None] * len(values)

    for index in range(period - 1, len(values)):
        window = values[index - period + 1 : index + 1]
        avg = mean(window)
        variance = mean([(value - avg) ** 2 for value in window])
        deviation = sqrt(variance)
        middle[index] = avg
        upper[index] = avg + stddev * deviation
        lower[index] = avg - stddev * deviation

    return BollingerBands(middle=middle, upper=upper, lower=lower)


def adx(candles: list[CandleData], period: int = 14) -> ADXResult:
    if period <= 0:
        raise ValueError("period must be positive")

    length = len(candles)
    result_adx: list[float | None] = [None] * length
    plus_di: list[float | None] = [None] * length
    minus_di: list[float | None] = [None] * length
    if length <= period * 2:
        return ADXResult(result_adx, plus_di, minus_di)

    highs = [float(candle.high) for candle in candles]
    lows = [float(candle.low) for candle in candles]
    closes = [float(candle.close) for candle in candles]

    true_ranges = [0.0]
    plus_dm = [0.0]
    minus_dm = [0.0]
    for index in range(1, length):
        high_diff = highs[index] - highs[index - 1]
        low_diff = lows[index - 1] - lows[index]
        true_ranges.append(max(highs[index] - lows[index], abs(highs[index] - closes[index - 1]), abs(lows[index] - closes[index - 1])))
        plus_dm.append(high_diff if high_diff > low_diff and high_diff > 0 else 0.0)
        minus_dm.append(low_diff if low_diff > high_diff and low_diff > 0 else 0.0)

    smoothed_tr = sum(true_ranges[1 : period + 1])
    smoothed_plus_dm = sum(plus_dm[1 : period + 1])
    smoothed_minus_dm = sum(minus_dm[1 : period + 1])
    dx_values: list[float | None] = [None] * length

    for index in range(period, length):
        if index > period:
            smoothed_tr = smoothed_tr - (smoothed_tr / period) + true_ranges[index]
            smoothed_plus_dm = smoothed_plus_dm - (smoothed_plus_dm / period) + plus_dm[index]
            smoothed_minus_dm = smoothed_minus_dm - (smoothed_minus_dm / period) + minus_dm[index]

        if smoothed_tr == 0:
            plus_di[index] = 0.0
            minus_di[index] = 0.0
            dx_values[index] = 0.0
            continue

        plus_di[index] = 100 * (smoothed_plus_dm / smoothed_tr)
        minus_di[index] = 100 * (smoothed_minus_dm / smoothed_tr)
        di_sum = plus_di[index] + minus_di[index]
        dx_values[index] = 0.0 if di_sum == 0 else 100 * abs(plus_di[index] - minus_di[index]) / di_sum

    first_adx_index = period * 2
    initial_dx = [value for value in dx_values[period : first_adx_index + 1] if value is not None]
    if len(initial_dx) == period + 1:
        result_adx[first_adx_index] = mean(initial_dx)
        for index in range(first_adx_index + 1, length):
            if dx_values[index] is not None and result_adx[index - 1] is not None:
                result_adx[index] = ((result_adx[index - 1] * (period - 1)) + dx_values[index]) / period

    return ADXResult(result_adx, plus_di, minus_di)


def candle_body_size(candle: CandleData) -> float:
    return abs(float(candle.close) - float(candle.open))


def upper_wick_ratio(candle: CandleData) -> float:
    candle_range = float(candle.high) - float(candle.low)
    if candle_range <= 0:
        return 0.0
    upper = float(candle.high) - max(float(candle.open), float(candle.close))
    return upper / candle_range


def lower_wick_ratio(candle: CandleData) -> float:
    candle_range = float(candle.high) - float(candle.low)
    if candle_range <= 0:
        return 0.0
    lower = min(float(candle.open), float(candle.close)) - float(candle.low)
    return lower / candle_range


def find_local_resistance(candles: list[CandleData], lookback: int = 50, tolerance_ratio: float = 0.001) -> PriceLevel | None:
    recent = candles[-lookback:]
    if len(recent) < 3:
        return None
    highs = [float(candle.high) for candle in recent]
    return _find_level(highs, tolerance_ratio, prefer="high")


def find_local_support(candles: list[CandleData], lookback: int = 50, tolerance_ratio: float = 0.001) -> PriceLevel | None:
    recent = candles[-lookback:]
    if len(recent) < 3:
        return None
    lows = [float(candle.low) for candle in recent]
    return _find_level(lows, tolerance_ratio, prefer="low")


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    relative_strength = avg_gain / avg_loss
    return 100 - (100 / (1 + relative_strength))


def _find_level(values: list[float], tolerance_ratio: float, prefer: str) -> PriceLevel | None:
    candidates: list[PriceLevel] = []
    for index, value in enumerate(values):
        tolerance = abs(value) * tolerance_ratio
        touches = sum(1 for candidate in values if abs(candidate - value) <= tolerance)
        candidates.append(PriceLevel(price=value, touches=touches, index=index))

    qualified = [level for level in candidates if level.touches >= 2]
    if qualified:
        return max(qualified, key=lambda level: level.price) if prefer == "high" else min(qualified, key=lambda level: level.price)
    return max(candidates, key=lambda level: level.touches, default=None)
