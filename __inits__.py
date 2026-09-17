from __future__ import annotations

from app.trading.strategies.base import BaseStrategy
from app.trading.strategies.bollinger_mean_reversion import BollingerMeanReversionStrategy
from app.trading.strategies.breakout_retest import BreakoutRetestStrategy
from app.trading.strategies.trend_pullback import TrendPullbackStrategy


STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    TrendPullbackStrategy.name: TrendPullbackStrategy,
    BollingerMeanReversionStrategy.name: BollingerMeanReversionStrategy,
    BreakoutRetestStrategy.name: BreakoutRetestStrategy,
}

ALL_STRATEGIES = "all"


def create_strategy(name: str) -> BaseStrategy:
    strategy_class = STRATEGY_REGISTRY.get(name)
    if strategy_class is None:
        raise UnknownStrategyError(name)
    return strategy_class()


def create_strategies(name: str) -> list[BaseStrategy]:
    if name == ALL_STRATEGIES:
        return [strategy_class() for strategy_class in STRATEGY_REGISTRY.values()]
    return [create_strategy(name)]


def available_strategy_names() -> list[str]:
    return [ALL_STRATEGIES, *sorted(STRATEGY_REGISTRY)]


def is_known_strategy_name(name: str) -> bool:
    return name == ALL_STRATEGIES or name in STRATEGY_REGISTRY


def validate_strategy_name(name: str) -> None:
    if not is_known_strategy_name(name):
        raise UnknownStrategyError(name)


class UnknownStrategyError(ValueError):
    def __init__(self, name: str) -> None:
        super().__init__(f"Unknown strategy: {name}")
        self.name = name
