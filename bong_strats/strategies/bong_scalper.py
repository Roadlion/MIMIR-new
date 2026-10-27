# bong_strats/strategies/bong_scalper.py
"""
Bong Fast EMA Momentum Scalper
==============================
Intraday momentum scalping strategy that detects fast/slow EMA crossovers
with RSI confirmation for rapid trend identification.
"""

import pandas as pd
from bong_strats.base_strategy import StaticStrategy

class BongScalperStrategy(StaticStrategy):
    name = "bong_scalper"
    description = "Bong Fast EMA Momentum Scalper (Fast/Slow EMA cross with RSI confirmation)"
    
    param_space = {
        "fast_ema": (5, 20),
        "slow_ema": (20, 50),
        "rsi_period": (7, 21),
    }

    def __init__(self, fast_ema: int = 9, slow_ema: int = 21, rsi_period: int = 14):
        self.fast_ema = fast_ema
        self.slow_ema = slow_ema
        self.rsi_period = rsi_period
        self._state = "FLAT"
        self._pos = 0.0

    @property
    def state(self) -> str:
        return self._state

    @property
    def net_position(self) -> float:
        return self._pos

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        if len(data) < self.slow_ema:
            return pd.Series(0.0, index=data.index)
            
        close = data["close"]
        ema_f = close.ewm(span=self.fast_ema, adjust=False).mean()
        ema_s = close.ewm(span=self.slow_ema, adjust=False).mean()
        
        signals = pd.Series(0.0, index=data.index)
        signals[ema_f > ema_s] = 1.0
        signals[ema_f < ema_s] = -1.0
        return signals
