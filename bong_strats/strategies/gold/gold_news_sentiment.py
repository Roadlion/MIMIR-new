# bong_strats/strategies/gold_news_sentiment.py
"""
Gold Macro & News Sentiment Strategy (XAU/USD)
==============================================
Trades Gold long or short based on macroeconomic news catalysts, central bank
sentiment (Fed, ECB, PBOC policy regime), geopolitical safe-haven flows, and
sentiment momentum, combined with protective volatility-based risk exits.

Rationale for Gold:
- Bullish Catalysts (Long):
    * Central bank dovish stance / rate cuts / quantitative easing
    * Geopolitical conflict / war / banking crisis (safe-haven flight)
    * Surging inflation expectations with declining real yields
    * Central bank gold accumulation news (e.g. PBOC, emerging markets)
- Bearish Catalysts (Short):
    * Central bank hawkish stance / rate hikes / quantitative tightening
    * Surging real yields (US 10Y TIPS) / strong US Dollar index (DXY)
    * Geopolitical de-escalation / risk-on rallies in equities
"""

from __future__ import annotations

import math
from typing import Dict, Any, List, Optional
import numpy as np
import pandas as pd
from bong_strats.base_strategy import StaticStrategy


class GoldNewsSentimentStrategy(StaticStrategy):
    name = "gold_news_sentiment"
    description = (
        "Gold Macro & News Sentiment Strategy (Long/Short XAU based on central bank "
        "policy, geopolitical safe-haven flows, sentiment momentum, and volatility stops)"
    )

    param_space = {
        # Long Parameters (Structural upward drift / Multi-day holding)
        "long_sentiment_threshold": (0.10, 0.45),
        "long_sl_atr_mult": (1.5, 3.5),
        "long_tp_atr_mult": (3.0, 7.0),
        
        # Short Parameters (Fast-strike liquidation flushes / Quick profit take)
        "short_sentiment_threshold": (0.15, 0.50),
        "short_sl_atr_mult": (1.0, 2.5),
        "short_tp_atr_mult": (1.5, 4.0),
        
        # Shared Market Filters
        "sentiment_decay_half_life": (5, 50),
        "rsi_overbought": (65.0, 85.0),
        "rsi_oversold": (15.0, 35.0),
        "exit_band": (0.02, 0.15),
    }

    def __init__(
        self,
        # Long thresholds (Tuned for secular bull trend continuation)
        long_sentiment_threshold: float = 0.22,
        long_sl_atr_mult: float = 2.0,
        long_tp_atr_mult: float = 4.5,
        
        # Short thresholds (Tuned for swift liquidation scalps before dip-buying)
        short_sentiment_threshold: float = 0.28,
        short_sl_atr_mult: float = 1.2,
        short_tp_atr_mult: float = 2.2,
        
        sentiment_decay_half_life: int = 15,
        rsi_overbought: float = 75.0,
        rsi_oversold: float = 25.0,
        exit_band: float = 0.05,
        mode: str = "adaptive",
        # Backward compatibility aliases
        sentiment_threshold: Optional[float] = None,
        stop_loss_atr_mult: Optional[float] = None,
        take_profit_atr_mult: Optional[float] = None,
        **kwargs: Any,
    ):
        # Allow backward-compatible overrides if generic params are passed
        self.long_sentiment_threshold = float(sentiment_threshold if sentiment_threshold is not None else long_sentiment_threshold)
        self.short_sentiment_threshold = float(sentiment_threshold if sentiment_threshold is not None else short_sentiment_threshold)
        
        self.long_sl_atr_mult = float(stop_loss_atr_mult if stop_loss_atr_mult is not None else long_sl_atr_mult)
        self.short_sl_atr_mult = float(stop_loss_atr_mult if stop_loss_atr_mult is not None else short_sl_atr_mult)
        
        self.long_tp_atr_mult = float(take_profit_atr_mult if take_profit_atr_mult is not None else long_tp_atr_mult)
        self.short_tp_atr_mult = float(take_profit_atr_mult if take_profit_atr_mult is not None else short_tp_atr_mult)
        
        self.sentiment_decay_half_life = int(sentiment_decay_half_life)
        self.rsi_overbought = float(rsi_overbought)
        self.rsi_oversold = float(rsi_oversold)
        self.exit_band = float(exit_band)
        self.mode = mode

        # Decay factor per tick/bar
        self._decay_lambda = math.log(2.0) / max(self.sentiment_decay_half_life, 1)

        # Internal State Machine
        self._state = "FLAT"
        self._pos = 0.0  # +1.0 for Long, -1.0 for Short, 0.0 for Flat
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._take_profit = 0.0
        self._trailing_stop = 0.0
        self._accumulated_sentiment = 0.0
        self._bars_in_trade = 0
        self._last_news_event = "NONE"

    @property
    def state(self) -> str:
        return self._state

    @property
    def net_position(self) -> float:
        return self._pos

    def _parse_macro_event(self, features: Dict[str, Any]) -> float:
        """Extract or score macro / news signals from features."""
        score = float(features.get("sentiment_score", 0.0))

        macro_signal = str(features.get("macro_signal", "")).lower().strip()
        if not macro_signal and "policy_signal" in features:
            macro_signal = str(features.get("policy_signal", "")).lower().strip()

        # Macro policy mapping specifically for Gold
        if macro_signal in ("dovish", "rate_cut", "easing", "qe"):
            score = max(score, 0.60)
            self._last_news_event = f"MACRO_DOVISH_{macro_signal.upper()}"
        elif macro_signal in ("hawkish", "rate_hike", "tightening", "qt"):
            score = min(score, -0.60)
            self._last_news_event = f"MACRO_HAWKISH_{macro_signal.upper()}"
        elif macro_signal in ("safe_haven", "geopolitical_risk", "war", "crisis", "gold_reserve_buying"):
            score = max(score, 0.75)
            self._last_news_event = f"SAFE_HAVEN_{macro_signal.upper()}"
        elif macro_signal in ("peace_talks", "deescalation", "risk_on"):
            score = min(score, -0.50)
            self._last_news_event = f"RISK_ON_{macro_signal.upper()}"

        return float(np.clip(score, -1.0, 1.0))

    def tick(self, price: float, features: Dict[str, Any]) -> List[str]:
        """Live bar execution state machine."""
        actions: List[str] = []
        if price <= 0:
            return actions

        # 1. Update accumulated sentiment with exponential time decay
        raw_sentiment = self._parse_macro_event(features)
        alpha = 0.35  # responsiveness weight to new headlines
        self._accumulated_sentiment = (
            self._accumulated_sentiment * math.exp(-self._decay_lambda) * (1.0 - alpha)
            + raw_sentiment * alpha
        )

        rsi = float(features.get("rsi_14", 50.0))
        atr = float(features.get("atr_14", price * 0.003))
        if atr <= 0:
            atr = price * 0.003

        # 2. Evaluate Active Position Exits & Risk Controls
        if self._pos > 0.0:
            self._bars_in_trade += 1
            # Update trailing stop once in favorable excursion
            if price > self._entry_price + (1.0 * atr):
                dynamic_trail = price - (self.long_sl_atr_mult * atr)
                self._trailing_stop = max(self._trailing_stop, dynamic_trail)

            # Exit Conditions for Long
            stop_level = max(self._stop_loss, self._trailing_stop)
            is_stopped = price <= stop_level
            is_tp = price >= self._take_profit
            # Sentiment flipped bearish or decayed into exit deadband with price failure
            sentiment_exhausted = (
                self._accumulated_sentiment < -self.exit_band
                or (self._accumulated_sentiment < self.exit_band and price < self._entry_price)
            )

            if is_stopped or is_tp or sentiment_exhausted:
                self._pos = 0.0
                self._state = "FLAT"
                self._bars_in_trade = 0
                actions.append("CLOSE_LONG")
                return actions

        elif self._pos < 0.0:
            self._bars_in_trade += 1
            # Update trailing stop once in favorable excursion
            if price < self._entry_price - (1.0 * atr):
                dynamic_trail = price + (self.short_sl_atr_mult * atr)
                self._trailing_stop = min(self._trailing_stop, dynamic_trail)

            # Exit Conditions for Short
            stop_level = min(self._stop_loss, self._trailing_stop) if self._trailing_stop > 0 else self._stop_loss
            is_stopped = price >= stop_level
            is_tp = price <= self._take_profit
            # Sentiment flipped bullish or decayed into exit deadband
            sentiment_exhausted = (
                self._accumulated_sentiment > self.exit_band
                or (self._accumulated_sentiment > -self.exit_band and price > self._entry_price)
            )

            if is_stopped or is_tp or sentiment_exhausted:
                self._pos = 0.0
                self._state = "FLAT"
                self._bars_in_trade = 0
                actions.append("CLOSE_SHORT")
                return actions

        # 3. Evaluate New Entries (when FLAT)
        if self._pos == 0.0:
            # Bullish Entry: Sentiment exceeds long threshold & technical RSI not exhausted
            if (
                self._accumulated_sentiment >= self.long_sentiment_threshold
                and rsi < self.rsi_overbought
            ):
                self._pos = 1.0
                self._state = "LONG"
                self._entry_price = price
                self._stop_loss = price - (self.long_sl_atr_mult * atr)
                self._take_profit = price + (self.long_tp_atr_mult * atr)
                self._trailing_stop = self._stop_loss
                self._bars_in_trade = 0
                actions.append("OPEN_LONG")

            # Bearish Entry: Sentiment drops below negative short threshold & RSI not exhausted
            elif (
                self._accumulated_sentiment <= -self.short_sentiment_threshold
                and rsi > self.rsi_oversold
            ):
                self._pos = -1.0
                self._state = "SHORT"
                self._entry_price = price
                self._stop_loss = price + (self.short_sl_atr_mult * atr)
                self._take_profit = price - (self.short_tp_atr_mult * atr)
                self._trailing_stop = self._stop_loss
                self._bars_in_trade = 0
                actions.append("OPEN_SHORT")

        return actions

    def status_line(self, price: float = 0.0) -> str:
        pos_str = "LONG" if self._pos > 0 else ("SHORT" if self._pos < 0 else "FLAT")
        pnl_pct = 0.0
        if self._pos > 0 and self._entry_price > 0:
            pnl_pct = (price - self._entry_price) / self._entry_price * 100
        elif self._pos < 0 and self._entry_price > 0:
            pnl_pct = (self._entry_price - price) / self._entry_price * 100

        return (
            f"GoldNews | Pos={pos_str} | Sent={self._accumulated_sentiment:+.3f} | "
            f"Event={self._last_news_event} | Entry={self._entry_price:.2f} | "
            f"SL={self._stop_loss:.2f} | TP={self._take_profit:.2f} | PnL={pnl_pct:+.2f}%"
        )

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        """
        Generates backtest signals Series (-1.0, 0.0, 1.0) aligned with data.index.
        Utilizes 'sentiment_score' column if present; otherwise computes a price-action
        momentum proxy for macro sentiment.
        """
        n = len(data)
        signals = pd.Series(0.0, index=data.index)
        if n < 20:
            return signals

        close = data["close"]
        high = data["high"] if "high" in data.columns else close
        low = data["low"] if "low" in data.columns else close

        # Compute ATR
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs()
        ], axis=1).max(axis=1)
        atr = tr.rolling(14, min_periods=1).mean().bfill()

        # Compute RSI
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14, min_periods=1).mean()
        loss = (-delta.clip(upper=0)).rolling(14, min_periods=1).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = (100 - (100 / (1 + rs))).fillna(50.0)

        # Macro sentiment series
        if "sentiment_score" in data.columns:
            raw_sent = data["sentiment_score"].fillna(0.0).values
        else:
            # Fallback momentum proxy: Fast vs Slow EWMA returns normalized
            fast_ma = close.ewm(span=12).mean()
            slow_ma = close.ewm(span=48).mean()
            spread = (fast_ma - slow_ma) / (atr.replace(0, 1.0))
            raw_sent = np.tanh(spread.values)

        # Vectorized / Bar-by-bar state replay with risk management
        pos = 0.0
        entry_price = 0.0
        stop_loss = 0.0
        take_profit = 0.0
        trailing_stop = 0.0
        accum_sent = 0.0
        decay = math.exp(-self._decay_lambda)
        alpha = 0.35

        for i in range(n):
            p = float(close.iloc[i])
            cur_atr = float(atr.iloc[i])
            cur_rsi = float(rsi.iloc[i])
            sent_in = float(raw_sent[i])

            accum_sent = accum_sent * decay * (1.0 - alpha) + sent_in * alpha

            # Position management
            if pos > 0.0:
                if p > entry_price + cur_atr:
                    trailing_stop = max(trailing_stop, p - self.long_sl_atr_mult * cur_atr)

                stop_lvl = max(stop_loss, trailing_stop)
                if (
                    p <= stop_lvl
                    or p >= take_profit
                    or accum_sent < -self.exit_band
                    or (accum_sent < self.exit_band and p < entry_price)
                ):
                    pos = 0.0
                    entry_price = 0.0

            elif pos < 0.0:
                if p < entry_price - cur_atr:
                    trailing_stop = min(trailing_stop, p + self.short_sl_atr_mult * cur_atr)

                stop_lvl = min(stop_loss, trailing_stop) if trailing_stop > 0 else stop_loss
                if (
                    p >= stop_lvl
                    or p <= take_profit
                    or accum_sent > self.exit_band
                    or (accum_sent > -self.exit_band and p > entry_price)
                ):
                    pos = 0.0
                    entry_price = 0.0

            # Entries
            if pos == 0.0:
                if accum_sent >= self.long_sentiment_threshold and cur_rsi < self.rsi_overbought:
                    pos = 1.0
                    entry_price = p
                    stop_loss = p - self.long_sl_atr_mult * cur_atr
                    take_profit = p + self.long_tp_atr_mult * cur_atr
                    trailing_stop = stop_loss
                elif accum_sent <= -self.short_sentiment_threshold and cur_rsi > self.rsi_oversold:
                    pos = -1.0
                    entry_price = p
                    stop_loss = p + self.short_sl_atr_mult * cur_atr
                    take_profit = p - self.short_tp_atr_mult * cur_atr
                    trailing_stop = stop_loss

            signals.iloc[i] = pos

        return signals
