from __future__ import annotations

from app.trading.candles import CandleData
from app.trading.indicators import candle_body_size, find_local_resistance, find_local_support
from app.trading.strategies.base import StrategySettings, StrategySignal


class BreakoutRetestStrategy:
    name = "breakout_retest"
    min_candles = 40

    lookback = 50
    tolerance_ratio = 0.0015
    min_touches = 2
    max_breakout_body_ratio = 0.008
    max_retest_distance_ratio = 0.002

    def generate_signal(
        self,
        asset: str,
        candles: list[CandleData],
        settings: StrategySettings,
    ) -> StrategySignal | None:
        if len(candles) < self.min_candles:
            return None

        level_candles = candles[-self.lookback - 2 : -2]
        if len(level_candles) < 3:
            return None

        breakout_candle = candles[-2]
        retest_candle = candles[-1]

        buy_signal = self._buy_signal(asset, level_candles, breakout_candle, retest_candle, settings)
        if buy_signal is not None:
            return buy_signal

        return self._sell_signal(asset, level_candles, breakout_candle, retest_candle, settings)

    def _buy_signal(
        self,
        asset: str,
        level_candles: list[CandleData],
        breakout_candle: CandleData,
        retest_candle: CandleData,
        settings: StrategySettings,
    ) -> StrategySignal | None:
        resistance = find_local_resistance(level_candles, self.lookback, self.tolerance_ratio)
        if resistance is None:
            return None

        level = resistance.price
        conditions = {
            "level_quality": resistance.touches >= self.min_touches,
            "breakout_close": float(breakout_candle.close) > level,
            "breakout_not_too_large": self._body_ratio(breakout_candle, level) <= self.max_breakout_body_ratio,
            "returned_to_level_from_above": float(retest_candle.low) <= self._upper_tolerance(level)
            and float(retest_candle.close) >= self._lower_tolerance(level),
            "bullish_confirmation": float(retest_candle.close) > float(retest_candle.open)
            and float(retest_candle.close) > level,
            "not_far_from_level": self._distance_ratio(float(retest_candle.close), level) <= self.max_retest_distance_ratio,
        }
        if not all(conditions.values()):
            return None
        return self._signal("buy", asset, retest_candle, settings, conditions, level, resistance.touches)

    def _sell_signal(
        self,
        asset: str,
        level_candles: list[CandleData],
        breakout_candle: CandleData,
        retest_candle: CandleData,
        settings: StrategySettings,
    ) -> StrategySignal | None:
        support = find_local_support(level_candles, self.lookback, self.tolerance_ratio)
        if support is None:
            return None

        level = support.price
        conditions = {
            "level_quality": support.touches >= self.min_touches,
            "breakout_close": float(breakout_candle.close) < level,
            "breakout_not_too_large": self._body_ratio(breakout_candle, level) <= self.max_breakout_body_ratio,
            "returned_to_level_from_below": float(retest_candle.high) >= self._lower_tolerance(level)
            and float(retest_candle.close) <= self._upper_tolerance(level),
            "bearish_confirmation": float(retest_candle.close) < float(retest_candle.open)
            and float(retest_candle.close) < level,
            "not_far_from_level": self._distance_ratio(float(retest_candle.close), level) <= self.max_retest_distance_ratio,
        }
        if not all(conditions.values()):
            return None
        return self._signal("sell", asset, retest_candle, settings, conditions, level, support.touches)

    def _signal(
        self,
        direction: str,
        asset: str,
        candle: CandleData,
        settings: StrategySettings,
        conditions: dict[str, bool],
        level: float,
        touches: int,
    ) -> StrategySignal:
        return StrategySignal(
            asset=asset,
            strategy_name=self.name,
            direction=direction,
            confidence=sum(conditions.values()) / len(conditions),
            reason={
                "conditions": conditions,
                "level": level,
                "touches": touches,
            },
            candle_timestamp=candle.timestamp,
            expiration_seconds=settings.expiration_seconds,
        )

    def _upper_tolerance(self, level: float) -> float:
        return level * (1 + self.tolerance_ratio)

    def _lower_tolerance(self, level: float) -> float:
        return level * (1 - self.tolerance_ratio)

    def _body_ratio(self, candle: CandleData, level: float) -> float:
        if level == 0:
            return 0.0
        return candle_body_size(candle) / abs(level)

    def _distance_ratio(self, price: float, level: float) -> float:
        if level == 0:
            return 0.0
        return abs(price - level) / abs(level)
