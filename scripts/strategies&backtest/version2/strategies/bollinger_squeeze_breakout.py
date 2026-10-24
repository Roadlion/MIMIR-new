# bong_strats/strategies/bollinger_squeeze_breakout.py
"""
Bollinger Squeeze Volatility Breakout Strategy.

Concept:
  - Detects volatility coiling when Bollinger Bandwidth is tight (< 0.04).
  - Fires long when candle closes above upper band during or immediately after squeeze.
  - Fires short when candle closes below lower band.
"""

from bong_strats.quick_strategy import QuickStrategy, BarIndicators
import pandas as pd


class BollingerSqueezeBreakoutStrategy(QuickStrategy):
    name = "bollinger_squeeze"
    description = "Bollinger Bandwidth squeeze breakout with ATR dynamic targets"

    bb_period: int = 20
    bb_std: float = 2.0
    squeeze_bandwidth_thresh: float = 0.05

    stop_loss_pct: float = 1.8
    take_profit_pct: float = 4.0
    trailing_stop_pct: float = 1.5

    def on_bar(self, bar: pd.Series, ind: BarIndicators):
        close = float(bar["close"])
        upper, mid, lower, bw = ind.bb

        # If bandwidth is expanding out of squeeze
        if bw > self.squeeze_bandwidth_thresh:
            if close > upper:
                return "BUY"
            elif close < lower:
                return "SELL"

        # Reversion to mean exit
        if self.net_position > 0 and close < mid:
            return "CLOSE"
        if self.net_position < 0 and close > mid:
            return "CLOSE"

        return "HOLD"
