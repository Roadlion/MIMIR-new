# bong_strats/strategies/arbitrage/stat_arb_kalman.py
"""
Pair Statistical Arbitrage APU Strategy
=======================================
Kalman-filtered cointegrated pairs trading with Ornstein-Uhlenbeck spread calibration.
"""
from bong_strats.static_strategies import PairStatArbitrageStrategy

__all__ = ["PairStatArbitrageStrategy"]
