import pytest

from app.trading.strategies import UnknownStrategyError, available_strategy_names, create_strategies, create_strategy
from app.trading.strategies.bollinger_mean_reversion import BollingerMeanReversionStrategy
from app.trading.strategies.breakout_retest import BreakoutRetestStrategy
from app.trading.strategies.trend_pullback import TrendPullbackStrategy


def test_create_strategy_returns_trend_pullback() -> None:
    strategy = create_strategy("trend_pullback")

    assert isinstance(strategy, TrendPullbackStrategy)


def test_create_strategy_returns_bollinger_mean_reversion() -> None:
    strategy = create_strategy("bollinger_mean_reversion")

    assert isinstance(strategy, BollingerMeanReversionStrategy)


def test_create_strategy_returns_breakout_retest() -> None:
    strategy = create_strategy("breakout_retest")

    assert isinstance(strategy, BreakoutRetestStrategy)


def test_create_strategy_rejects_unknown_strategy() -> None:
    with pytest.raises(UnknownStrategyError):
        create_strategy("unknown")


def test_create_strategies_returns_all_strategies() -> None:
    strategies = create_strategies("all")

    assert [strategy.name for strategy in strategies] == [
        "trend_pullback",
        "bollinger_mean_reversion",
        "breakout_retest",
    ]


def test_available_strategy_names_include_all_mode() -> None:
    assert available_strategy_names() == ["all", "bollinger_mean_reversion", "breakout_retest", "trend_pullback"]
