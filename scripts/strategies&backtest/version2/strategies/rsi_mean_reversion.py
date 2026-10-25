# bong_strats/strategies/rsi_mean_reversion.py
"""
RSI Trend-Filtered Mean Reversion Strategy.

Concept:
  - Buys high-probability dips when RSI < 30 but overall macro trend is bullish (Price > EMA 200).
  - Short counter-trend bounces when RSI > 70 and Price < EMA 200.
  - Built-in 1.5% Stop Loss and 3.5% Take Profit.
"""

from bong_strats.quick_strategy import QuickStrategy, BarIndicators
import pandas as pd


class RSIMeanReversionStrategy(QuickStrategy):
    name = "rsi_mean_reversion"
    description = "RSI dip-buyer with 200 EMA trend filter and dynamic SL/TP brackets"

    # Strategy Parameters (tunable)
    rsi_period: int = 14
    oversold_thresh: float = 32.0
    overbought_thresh: float = 68.0
    trend_ema_period: int = 200

    # Risk Parameters (%)
    stop_loss_pct: float = 1.5
    take_profit_pct: float = 3.5
    trailing_stop_pct: float = 1.2

    def on_bar(self, bar: pd.Series, ind: BarIndicators):
        close = float(bar["close"])
        rsi_val = ind.rsi_n(self.rsi_period)
        trend_ema = ind.ema_n(self.trend_ema_period)

        # Bullish Dip: Bull trend + oversold RSI
        if rsi_val < self.oversold_thresh and close > trend_ema:
            return "BUY"

        # Bearish Exhaustion: Bear trend + overbought RSI
        if rsi_val > self.overbought_thresh and close < trend_ema:
            return "SELL"

        # Normal profit taking / mean reversion exit
        if rsi_val >= 55.0 and self.net_position > 0:
            return "CLOSE"
        if rsi_val <= 45.0 and self.net_position < 0:
            return "CLOSE"

        return "HOLD"
