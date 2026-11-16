# bong_strats/strategies/arbitrage/dual_hedge.py
"""
Dual Hedge Statistical Hedging Strategy
=======================================
Dual-leg hedging state machine (FLAT -> INITIAL -> HEDGED -> SOLO).
"""
from bong_strats.static_strategies import DualHedgeStrategy

__all__ = ["DualHedgeStrategy"]
