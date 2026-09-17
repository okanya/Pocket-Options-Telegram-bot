from __future__ import annotations

from app.trading.candles import CandleData
from app.trading.indicators import adx, bollinger_bands, rsi
from app.trading.strategies.base import StrategySettings, StrategySignal


class BollingerMeanReversionStrategy:
    name = "bollinger_mean_reversion"
    min_candles = 35

    bollinger_period = 20
    bollinger_stddev = 2.0
    rsi_period = 14
    adx_period = 14
    max_adx = 25.0
    buy_rsi_max = 35.0
    sell_rsi_min = 65.0

    def generate_signal(
        self,
        asset: str,
        candles: list[CandleData],
        settings: StrategySettings,
    ) -> StrategySignal | None:
        if len(candles) < self.min_candles:
            return None

        closes = [float(candle.close) for candle in candles]
        bands = bollinger_bands(closes, self.bollinger_period, self.bollinger_stddev)
        rsi_14 = rsi(closes, self.rsi_period)
        adx_14 = adx(candles, self.adx_period)

        latest_index = len(candles) - 1
        previous_index = latest_index - 1
        required_values = (
            bands.upper[latest_index],
            bands.lower[latest_index],
            bands.upper[previous_index],
            bands.lower[previous_index],
            rsi_14[latest_index],
            adx_14.adx[latest_index],
        )
        if any(value is None for value in required_values):
            return None

        latest_close = closes[latest_index]
        previous_candle = candles[previous_index]
        latest_candle = candles[latest_index]

        buy_conditions = {
            "adx_ranging": adx_14.adx[latest_index] <= self.max_adx,
            "previous_touched_lower_band": float(previous_candle.low) <= bands.lower[previous_index],
            "rsi_oversold": rsi_14[latest_index] <= self.buy_rsi_max,
            "closed_back_inside_bands": latest_close > bands.lower[latest_index] and latest_close < bands.upper[latest_index],
        }
        if all(buy_conditions.values()):
            return self._signal("buy", asset, latest_candle, settings, buy_conditions)

        sell_conditions = {
            "adx_ranging": adx_14.adx[latest_index] <= self.max_adx,
            "previous_touched_upper_band": float(previous_candle.high) >= bands.upper[previous_index],
            "rsi_overbought": rsi_14[latest_index] >= self.sell_rsi_min,
            "closed_back_inside_bands": latest_close < bands.upper[latest_index] and latest_close > bands.lower[latest_index],
        }
        if all(sell_conditions.values()):
            return self._signal("sell", asset, latest_candle, settings, sell_conditions)

        return None

    def _signal(
        self,
        direction: str,
        asset: str,
        candle: CandleData,
        settings: StrategySettings,
        conditions: dict[str, bool],
    ) -> StrategySignal:
        return StrategySignal(
            asset=asset,
            strategy_name=self.name,
            direction=direction,
            confidence=sum(conditions.values()) / len(conditions),
            reason={"conditions": conditions},
            candle_timestamp=candle.timestamp,
            expiration_seconds=settings.expiration_seconds,
        )
