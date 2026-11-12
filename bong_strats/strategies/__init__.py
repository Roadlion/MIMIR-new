# bong_strats/strategies/__init__.py
"""
MIMIR Strategy Hub: Organized Topic Modules
"""
from .gold import GoldMathScalperStrategy, GoldNewsSentimentStrategy
from .momentum import BongScalperStrategy, VolumeExhaustionReversalStrategy
from .mean_reversion import MeanReversionStrategy, BollingerBandStrategy
from .arbitrage import DualHedgeStrategy, PairStatArbitrageStrategy
from .flagship import WarRigStrategy

__all__ = [
    "GoldMathScalperStrategy",
    "GoldNewsSentimentStrategy",
    "BongScalperStrategy",
    "VolumeExhaustionReversalStrategy",
    "MeanReversionStrategy",
    "BollingerBandStrategy",
    "DualHedgeStrategy",
    "PairStatArbitrageStrategy",
    "WarRigStrategy",
]
