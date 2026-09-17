from __future__ import annotations

from app.trading.candles import CandleData
from app.trading.indicators import ema, rsi
from app.trading.strategies.base import StrategySettings, StrategySignal


class TrendPullbackStrategy:
    name = "trend_pullback"
    min_candles = 60

    def generate_signal(
        self,
        asset: str,
        candles: list[CandleData],
        settings: StrategySettings,
    ) -> StrategySignal | None:
        if len(candles) < self.min_candles:
            return None

        closes = [float(candle.close) for candle in candles]
        ema_9 = ema(closes, 9)
        ema_21 = ema(closes, 21)
        ema_50 = ema(closes, 50)
        rsi_14 = rsi(closes, 14)

        latest_index = len(candles) - 1
        previous_index = latest_index - 1
        if any(
            value is None
            for value in (
                ema_9[latest_index],
                ema_21[latest_index],
                ema_50[latest_index],
                ema_50[previous_index],
                rsi_14[latest_index],
                rsi_14[previous_index],
            )
        ):
            return None

        latest_close = closes[latest_index]
        latest_candle = candles[latest_index]

        buy_conditions = {
            "ema50_rising": ema_50[latest_index] > ema_50[previous_index],
            "price_above_ema50": latest_close > ema_50[latest_index],
            "ema9_above_ema21": ema_9[latest_index] > ema_21[latest_index],
            "rsi_pullback_rising": 40 <= rsi_14[previous_index] <= 50 and rsi_14[latest_index] > rsi_14[previous_index],
            "close_above_fast_ema": latest_close > ema_9[latest_index] or latest_close > ema_21[latest_index],
        }
        if all(buy_conditions.values()):
            return self._signal("buy", asset, latest_candle, settings, buy_conditions)

        sell_conditions = {
            "ema50_falling": ema_50[latest_index] < ema_50[previous_index],
            "price_below_ema50": latest_close < ema_50[latest_index],
            "ema9_below_ema21": ema_9[latest_index] < ema_21[latest_index],
            "rsi_pullback_falling": 50 <= rsi_14[previous_index] <= 60 and rsi_14[latest_index] < rsi_14[previous_index],
            "close_below_fast_ema": latest_close < ema_9[latest_index] or latest_close < ema_21[latest_index],
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
