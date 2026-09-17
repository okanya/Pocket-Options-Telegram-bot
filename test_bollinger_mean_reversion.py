from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.trading.candles import CandleData
from app.trading.indicators import ADXResult, BollingerBands
from app.trading.strategies.base import StrategySettings
from app.trading.strategies.bollinger_mean_reversion import BollingerMeanReversionStrategy


def test_bollinger_mean_reversion_ignores_insufficient_candles() -> None:
    strategy = BollingerMeanReversionStrategy()

    signal = strategy.generate_signal(
        "EURUSD_otc",
        _candles(34),
        StrategySettings(expiration_seconds=60, timeframe_seconds=60),
    )

    assert signal is None


def test_bollinger_mean_reversion_generates_buy_signal(monkeypatch) -> None:
    strategy = BollingerMeanReversionStrategy()
    candles = _candles(35, close=100)
    candles[-2] = _candle(33, open_price=100, high=101, low=94, close=95)
    candles[-1] = _candle(34, open_price=96, high=100, low=96, close=98)

    monkeypatch.setattr("app.trading.strategies.bollinger_mean_reversion.bollinger_bands", _bands)
    monkeypatch.setattr("app.trading.strategies.bollinger_mean_reversion.rsi", lambda values, period: _series(len(values), 30))
    monkeypatch.setattr("app.trading.strategies.bollinger_mean_reversion.adx", lambda candles, period: _adx(len(candles), 18))

    signal = strategy.generate_signal(
        "EURUSD_otc",
        candles,
        StrategySettings(expiration_seconds=120, timeframe_seconds=60),
    )

    assert signal is not None
    assert signal.strategy_name == "bollinger_mean_reversion"
    assert signal.direction == "buy"
    assert signal.expiration_seconds == 120
    assert signal.confidence == 1


def test_bollinger_mean_reversion_generates_sell_signal(monkeypatch) -> None:
    strategy = BollingerMeanReversionStrategy()
    candles = _candles(35, close=100)
    candles[-2] = _candle(33, open_price=100, high=106, low=99, close=105)
    candles[-1] = _candle(34, open_price=104, high=104, low=100, close=102)

    monkeypatch.setattr("app.trading.strategies.bollinger_mean_reversion.bollinger_bands", _bands)
    monkeypatch.setattr("app.trading.strategies.bollinger_mean_reversion.rsi", lambda values, period: _series(len(values), 70))
    monkeypatch.setattr("app.trading.strategies.bollinger_mean_reversion.adx", lambda candles, period: _adx(len(candles), 20))

    signal = strategy.generate_signal(
        "EURUSD_otc",
        candles,
        StrategySettings(expiration_seconds=60, timeframe_seconds=60),
    )

    assert signal is not None
    assert signal.direction == "sell"
    assert signal.confidence == 1


def test_bollinger_mean_reversion_returns_none_when_adx_is_too_high(monkeypatch) -> None:
    strategy = BollingerMeanReversionStrategy()
    candles = _candles(35, close=100)
    candles[-2] = _candle(33, open_price=100, high=101, low=94, close=95)
    candles[-1] = _candle(34, open_price=96, high=100, low=96, close=98)

    monkeypatch.setattr("app.trading.strategies.bollinger_mean_reversion.bollinger_bands", _bands)
    monkeypatch.setattr("app.trading.strategies.bollinger_mean_reversion.rsi", lambda values, period: _series(len(values), 30))
    monkeypatch.setattr("app.trading.strategies.bollinger_mean_reversion.adx", lambda candles, period: _adx(len(candles), 30))

    signal = strategy.generate_signal(
        "EURUSD_otc",
        candles,
        StrategySettings(expiration_seconds=60, timeframe_seconds=60),
    )

    assert signal is None


def _candles(count: int, close: float = 100) -> list[CandleData]:
    return [_candle(index, open_price=close, high=close + 1, low=close - 1, close=close) for index in range(count)]


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


def _bands(values, period, stddev):
    count = len(values)
    middle = [None] * count
    upper = [None] * count
    lower = [None] * count
    upper[-2] = 105
    upper[-1] = 105
    lower[-2] = 95
    lower[-1] = 95
    middle[-2] = 100
    middle[-1] = 100
    return BollingerBands(middle=middle, upper=upper, lower=lower)


def _series(count: int, latest: float) -> list[float | None]:
    values: list[float | None] = [None] * count
    values[-1] = latest
    return values


def _adx(count: int, latest: float) -> ADXResult:
    values: list[float | None] = [None] * count
    values[-1] = latest
    return ADXResult(adx=values, plus_di=values.copy(), minus_di=values.copy())
