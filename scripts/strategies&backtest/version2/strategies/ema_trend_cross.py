# bong_strats/strategies/ema_trend_cross.py
"""
Fast/Slow EMA Trend Following Strategy.

Concept:
  - 9 EMA crosses above 21 EMA while price is above 50 EMA -> BUY (Long Momentum).
  - 9 EMA crosses below 21 EMA -> CLOSE or SHORT.
  - Built-in ATR-anchored Stop Loss and Take Profit.
"""

from bong_strats.quick_strategy import QuickStrategy, BarIndicators
import pandas as pd


class EMATrendCrossStrategy(QuickStrategy):
    name = "ema_trend_cross"
    description = "9/21 EMA crossover with 50 EMA trend slope confirmation and ATR stops"

    fast_ema: int = 9
    slow_ema: int = 21
    trend_ema: int = 50

    stop_loss_pct: float = 2.0
    take_profit_pct: float = 5.0
    trailing_stop_pct: float = 1.5

    def on_bar(self, bar: pd.Series, ind: BarIndicators):
        close = float(bar["close"])
        fast = ind.ema_n(self.fast_ema)
        slow = ind.ema_n(self.slow_ema)
        baseline = ind.ema_n(self.trend_ema)

        # Bullish momentum cross above baseline
        if fast > slow and close > baseline:
            return "BUY"

        # Bearish exit / short trigger
        if fast < slow and close < baseline:
            return "SELL"
        elif fast < slow and self.net_position > 0:
            return "CLOSE"

        return "HOLD"
