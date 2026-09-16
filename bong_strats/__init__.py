# backend/app/analytics/user_strategies/__init__.py
from .base_strategy import Strategy, StaticStrategy
from .static_strategies import (
    DualHedgeStrategy,
    MeanReversionStrategy,
    BollingerBandStrategy,
    VolumeExhaustionReversalStrategy,
    USER_STRATEGY_REGISTRY,
    BACKTEST_STRATEGY_REGISTRY,
)
from .strategy_runner import run_user_strategies

__all__ = [
    "Strategy",
    "StaticStrategy",
    "DualHedgeStrategy",
    "MeanReversionStrategy",
    "BollingerBandStrategy",
    "VolumeExhaustionReversalStrategy",
    "USER_STRATEGY_REGISTRY",
    "BACKTEST_STRATEGY_REGISTRY",
    "run_user_strategies",
]
