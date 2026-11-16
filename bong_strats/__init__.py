# bong_strats/__init__.py
from backend.app.analytics.user_strategies.base_strategy import Strategy, StaticStrategy
from backend.app.analytics.user_strategies.quick_strategy import QuickStrategy, IndicatorHelper, BarIndicators

__all__ = [
    "Strategy",
    "StaticStrategy",
    "QuickStrategy",
    "IndicatorHelper",
    "BarIndicators",
    "DualHedgeStrategy",
    "MeanReversionStrategy",
    "BollingerBandStrategy",
    "VolumeExhaustionReversalStrategy",
    "PairStatArbitrageStrategy",
    "USER_STRATEGY_REGISTRY",
    "BACKTEST_STRATEGY_REGISTRY",
    "FULL_STRATEGY_REGISTRY",
    "discover_strategies",
    "reload_registries",
    "list_strategies",
    "run_user_strategies",
]

def __getattr__(name):
    import backend.app.analytics.user_strategies.registry as reg
    if hasattr(reg, name):
        return getattr(reg, name)
    import backend.app.analytics.user_strategies.static_strategies as ss
    if hasattr(ss, name):
        return getattr(ss, name)
    import backend.app.analytics.user_strategies.strategy_runner as sr
    if hasattr(sr, name):
        return getattr(sr, name)
    raise AttributeError(f"module 'bong_strats' has no attribute '{name}'")
