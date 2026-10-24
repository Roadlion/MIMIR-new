# bong_strats/quick_strategy.py
"""
High-velocity base class for creating quantitative trading strategies in MIMIR.
Eliminates boilerplate: write your logic in ~15-20 lines, and get both:
  1. Vectorized / bar-by-bar backtesting with metrics and SL/TP.
  2. Live MT5 tick-by-tick state machine execution with broker sync.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import numpy as np
import pandas as pd

from .base_strategy import StaticStrategy
from .indicators import (
    OUCalibrator,
    VolatilityModel,
    compute_atr,
    compute_bollinger_features,
    compute_ou_features,
    compute_rsi,
    compute_volatility_regime,
)


class IndicatorHelper:
    """Convenient, cached technical indicator suite for OHLCV data."""

    def __init__(self, data: pd.DataFrame):
        self._df = data.copy()
        # Normalize column names to lowercase
        self._df.columns = [str(c).lower().strip() for c in self._df.columns]
        self._cache: Dict[str, Any] = {}

    @property
    def close(self) -> pd.Series:
        return self._df["close"]

    @property
    def high(self) -> pd.Series:
        return self._df.get("high", self._df["close"])

    @property
    def low(self) -> pd.Series:
        return self._df.get("low", self._df["close"])

    @property
    def open(self) -> pd.Series:
        return self._df.get("open", self._df["close"])

    @property
    def volume(self) -> pd.Series:
        return self._df.get("volume", pd.Series(1.0, index=self._df.index))

    def rsi(self, period: int = 14) -> pd.Series:
        key = f"rsi_{period}"
        if key not in self._cache:
            self._cache[key] = compute_rsi(self.close, period=period)
        return self._cache[key]

    def ema(self, period: int = 20) -> pd.Series:
        key = f"ema_{period}"
        if key not in self._cache:
            self._cache[key] = self.close.ewm(span=period, adjust=False).mean()
        return self._cache[key]

    def sma(self, period: int = 50) -> pd.Series:
        key = f"sma_{period}"
        if key not in self._cache:
            self._cache[key] = self.close.rolling(window=period, min_periods=1).mean()
        return self._cache[key]

    def atr(self, period: int = 14) -> pd.Series:
        key = f"atr_{period}"
        if key not in self._cache:
            self._cache[key] = compute_atr(self.high, self.low, self.close, period=period)
        return self._cache[key]

    def bollinger(self, period: int = 20, num_std: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
        """Returns (upper, mid, lower, bandwidth)."""
        key = f"bb_{period}_{num_std}"
        if key not in self._cache:
            mid = self.close.rolling(window=period, min_periods=period).mean()
            std = self.close.rolling(window=period, min_periods=period).std()
            upper = mid + (num_std * std)
            lower = mid - (num_std * std)
            bandwidth = (upper - lower) / mid.replace(0, np.nan)
            self._cache[key] = (upper, mid, lower, bandwidth)
        return self._cache[key]

    def macd(self, fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """Returns (macd_line, signal_line, histogram)."""
        key = f"macd_{fast}_{slow}_{signal}"
        if key not in self._cache:
            ema_fast = self.close.ewm(span=fast, adjust=False).mean()
            ema_slow = self.close.ewm(span=slow, adjust=False).mean()
            macd_line = ema_fast - ema_slow
            sig_line = macd_line.ewm(span=signal, adjust=False).mean()
            hist = macd_line - sig_line
            self._cache[key] = (macd_line, sig_line, hist)
        return self._cache[key]

    def ou_zscore(self, lookback: int = 30) -> pd.Series:
        """Rolling Ornstein-Uhlenbeck z-score."""
        key = f"ou_zscore_{lookback}"
        if key not in self._cache:
            df_feat = compute_ou_features(self.close, window=lookback)
            self._cache[key] = df_feat["ou_zscore"]
        return self._cache[key]

    def donchian(self, period: int = 20) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """Returns (upper, lower, mid) channels."""
        key = f"donchian_{period}"
        if key not in self._cache:
            upper = self.high.rolling(window=period, min_periods=1).max()
            lower = self.low.rolling(window=period, min_periods=1).min()
            mid = (upper + lower) / 2.0
            self._cache[key] = (upper, lower, mid)
        return self._cache[key]

    def stoch(self, k_period: int = 14, d_period: int = 3) -> Tuple[pd.Series, pd.Series]:
        """Returns (Fast %K, Slow %D)."""
        key = f"stoch_{k_period}_{d_period}"
        if key not in self._cache:
            low_min = self.low.rolling(window=k_period, min_periods=1).min()
            high_max = self.high.rolling(window=k_period, min_periods=1).max()
            diff = (high_max - low_min).replace(0, np.nan)
            k = 100 * ((self.close - low_min) / diff)
            d = k.rolling(window=d_period, min_periods=1).mean()
            self._cache[key] = (k, d)
        return self._cache[key]


class BarIndicators:
    """Lightweight indicator snapshot passed to on_bar()."""

    def __init__(self, helper: IndicatorHelper, index_pos: int):
        self._helper = helper
        self._idx = index_pos

    def _val(self, series: pd.Series) -> float:
        val = series.iloc[self._idx]
        return float(val) if pd.notna(val) else 0.0

    @property
    def rsi(self) -> float:
        return self._val(self._helper.rsi(14))

    def rsi_n(self, period: int = 14) -> float:
        return self._val(self._helper.rsi(period))

    @property
    def ema_9(self) -> float:
        return self._val(self._helper.ema(9))

    @property
    def ema_20(self) -> float:
        return self._val(self._helper.ema(20))

    @property
    def ema_50(self) -> float:
        return self._val(self._helper.ema(50))

    @property
    def ema_200(self) -> float:
        return self._val(self._helper.ema(200))

    def ema_n(self, period: int) -> float:
        return self._val(self._helper.ema(period))

    def sma_n(self, period: int) -> float:
        return self._val(self._helper.sma(period))

    @property
    def atr(self) -> float:
        return self._val(self._helper.atr(14))

    def atr_n(self, period: int = 14) -> float:
        return self._val(self._helper.atr(period))

    @property
    def bb(self) -> Tuple[float, float, float, float]:
        """(upper, mid, lower, bandwidth)"""
        u, m, l, bw = self._helper.bollinger(20, 2.0)
        return self._val(u), self._val(m), self._val(l), self._val(bw)

    @property
    def macd(self) -> Tuple[float, float, float]:
        """(macd, signal, hist)"""
        m, s, h = self._helper.macd(12, 26, 9)
        return self._val(m), self._val(s), self._val(h)

    @property
    def ou_zscore(self) -> float:
        return self._val(self._helper.ou_zscore(30))

    def donchian(self, period: int = 20) -> Tuple[float, float, float]:
        u, l, m = self._helper.donchian(period)
        return self._val(u), self._val(l), self._val(m)


class QuickStrategy(StaticStrategy):
    """
    High-Velocity Strategy Base Class.

    Subclasses can simply specify:
      name: str = "my_strat"
      description: str = "Brief explanation"

      # Optional Risk parameters (in %):
      stop_loss_pct: float = 0.0   # e.g., 1.5 for 1.5% SL
      take_profit_pct: float = 0.0  # e.g., 3.0 for 3.0% TP
      trailing_stop_pct: float = 0.0

      # Parameters declared as class/instance attributes are automatically
      # exposed in param_space for optimization!

    And implement EITHER:
      1) on_bar(self, bar, indicators) -> "BUY" | "SELL" | "CLOSE" | "HOLD" | 1 | -1 | 0
      OR
      2) generate_signals(self, df: pd.DataFrame) -> pd.Series
    """

    name: str = "quick_strategy"
    description: str = "QuickStrategy Base"

    # Default Risk Controls
    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0
    trailing_stop_pct: float = 0.0
    is_live_ready: bool = True

    def __init__(self, **kwargs):
        # Auto-initialize declared params
        for k, v in kwargs.items():
            setattr(self, k, v)

        # Internal live state tracking
        self._live_state: str = "FLAT"
        self._current_pos: float = 0.0
        self._entry_price: float = 0.0
        self._peak_price: float = 0.0
        self._trough_price: float = float("inf")
        self._recent_bars: List[Dict[str, float]] = []

    @property
    def state(self) -> str:
        return self._live_state

    @property
    def net_position(self) -> float:
        return self._current_pos

    @classmethod
    def get_declared_params(cls) -> Dict[str, Any]:
        """Auto-detect numerical parameters defined on the strategy."""
        params = {}
        for k, v in cls.__dict__.items():
            if not k.startswith("_") and isinstance(v, (int, float, bool)) and k not in ("is_live_ready",):
                params[k] = v
        return params

    def get_params(self) -> dict:
        declared = self.get_declared_params()
        return {k: getattr(self, k, v) for k, v in declared.items()}

    def set_params(self, params: dict) -> None:
        for k, v in params.items():
            setattr(self, k, v)

    def on_bar(self, bar: pd.Series, indicators: BarIndicators) -> Optional[Union[str, int]]:
        """
        Optional event-driven callback called on each bar.
        Override this OR generate_signals().

        Returns:
          "BUY" / 1: Go long (or enter long)
          "SELL" / -1: Go short (or enter short)
          "CLOSE" / 0: Close any open position
          "HOLD" / None: Keep current position unchanged
        """
        return None

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        """
        Generate backtest signals {-1, 0, 1} aligned to data.index.
        If subclass did not override this, it executes on_bar() sequentially
        with full SL/TP simulation.
        """
        if data.empty:
            return pd.Series(dtype=float)

        helper = IndicatorHelper(data)
        n = len(data)
        signals = np.zeros(n, dtype=float)

        pos = 0.0
        entry_price = 0.0
        peak_price = 0.0
        trough_price = float("inf")

        sl_pct = getattr(self, "stop_loss_pct", 0.0) / 100.0
        tp_pct = getattr(self, "take_profit_pct", 0.0) / 100.0
        trail_pct = getattr(self, "trailing_stop_pct", 0.0) / 100.0

        close_prices = helper.close.values
        high_prices = helper.high.values
        low_prices = helper.low.values

        for i in range(n):
            curr_close = close_prices[i]
            curr_high = high_prices[i]
            curr_low = low_prices[i]

            # 1. Check Exit / SL / TP if currently in position
            if pos > 0:  # LONG
                peak_price = max(peak_price, curr_high)
                # Stop loss
                if sl_pct > 0 and curr_low <= entry_price * (1.0 - sl_pct):
                    pos = 0.0
                # Take profit
                elif tp_pct > 0 and curr_high >= entry_price * (1.0 + tp_pct):
                    pos = 0.0
                # Trailing stop
                elif trail_pct > 0 and curr_low <= peak_price * (1.0 - trail_pct):
                    pos = 0.0
            elif pos < 0:  # SHORT
                trough_price = min(trough_price, curr_low)
                # Stop loss
                if sl_pct > 0 and curr_high >= entry_price * (1.0 + sl_pct):
                    pos = 0.0
                # Take profit
                elif tp_pct > 0 and curr_low <= entry_price * (1.0 - tp_pct):
                    pos = 0.0
                # Trailing stop
                elif trail_pct > 0 and curr_high >= trough_price * (1.0 + trail_pct):
                    pos = 0.0

            # 2. Evaluate Strategy on_bar logic
            bar = data.iloc[i]
            bar_ind = BarIndicators(helper, i)
            action = self.on_bar(bar, bar_ind)

            if action is not None:
                if action in ("BUY", 1, "OPEN_LONG"):
                    if pos <= 0:
                        pos = 1.0
                        entry_price = curr_close
                        peak_price = curr_close
                elif action in ("SELL", -1, "OPEN_SHORT"):
                    if pos >= 0:
                        pos = -1.0
                        entry_price = curr_close
                        trough_price = curr_close
                elif action in ("CLOSE", 0, "EXIT", "FLAT"):
                    pos = 0.0

            signals[i] = pos

        return pd.Series(signals, index=data.index)

    def tick(self, price: float, features: dict) -> list[str]:
        """
        Live tick state machine: advances strategy state by one tick.
        Emits list of actions: ["OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT"].
        """
        actions = []
        if price <= 0:
            return actions

        sl_pct = getattr(self, "stop_loss_pct", 0.0) / 100.0
        tp_pct = getattr(self, "take_profit_pct", 0.0) / 100.0
        trail_pct = getattr(self, "trailing_stop_pct", 0.0) / 100.0

        # Check existing SL/TP on live position
        if self._current_pos > 0:  # LONG
            self._peak_price = max(self._peak_price, price)
            if sl_pct > 0 and price <= self._entry_price * (1.0 - sl_pct):
                actions.append("CLOSE_LONG")
                self._current_pos = 0.0
                self._live_state = "FLAT"
                return actions
            if tp_pct > 0 and price >= self._entry_price * (1.0 + tp_pct):
                actions.append("CLOSE_LONG")
                self._current_pos = 0.0
                self._live_state = "FLAT"
                return actions
            if trail_pct > 0 and price <= self._peak_price * (1.0 - trail_pct):
                actions.append("CLOSE_LONG")
                self._current_pos = 0.0
                self._live_state = "FLAT"
                return actions

        elif self._current_pos < 0:  # SHORT
            self._trough_price = min(self._trough_price, price)
            if sl_pct > 0 and price >= self._entry_price * (1.0 + sl_pct):
                actions.append("CLOSE_SHORT")
                self._current_pos = 0.0
                self._live_state = "FLAT"
                return actions
            if tp_pct > 0 and price <= self._entry_price * (1.0 - tp_pct):
                actions.append("CLOSE_SHORT")
                self._current_pos = 0.0
                self._live_state = "FLAT"
                return actions
            if trail_pct > 0 and price >= self._trough_price * (1.0 + trail_pct):
                actions.append("CLOSE_SHORT")
                self._current_pos = 0.0
                self._live_state = "FLAT"
                return actions

        # Append tick to recent price window
        self._recent_bars.append({
            "close": price,
            "high": price,
            "low": price,
            "open": price,
            "volume": features.get("volume", 1.0),
        })
        if len(self._recent_bars) > 300:
            self._recent_bars.pop(0)

        # Evaluate on_bar or generate_signals on live price history
        if len(self._recent_bars) >= 15:
            df = pd.DataFrame(self._recent_bars)
            helper = IndicatorHelper(df)
            bar = df.iloc[-1]
            bar_ind = BarIndicators(helper, len(df) - 1)

            sig = self.on_bar(bar, bar_ind)
            if sig is None:
                # Fallback to generate_signals
                s_series = self.generate_signals(df)
                if not s_series.empty:
                    val = s_series.iloc[-1]
                    sig = "BUY" if val > 0 else ("SELL" if val < 0 else "CLOSE" if val == 0 else "HOLD")

            if sig in ("BUY", 1, "OPEN_LONG"):
                if self._current_pos <= 0:
                    if self._current_pos < 0:
                        actions.append("CLOSE_SHORT")
                    actions.append("OPEN_LONG")
                    self._current_pos = 1.0
                    self._entry_price = price
                    self._peak_price = price
                    self._live_state = "LONG"
            elif sig in ("SELL", -1, "OPEN_SHORT"):
                if self._current_pos >= 0:
                    if self._current_pos > 0:
                        actions.append("CLOSE_LONG")
                    actions.append("OPEN_SHORT")
                    self._current_pos = -1.0
                    self._entry_price = price
                    self._trough_price = price
                    self._live_state = "SHORT"
            elif sig in ("CLOSE", 0, "EXIT", "FLAT"):
                if self._current_pos > 0:
                    actions.append("CLOSE_LONG")
                elif self._current_pos < 0:
                    actions.append("CLOSE_SHORT")
                self._current_pos = 0.0
                self._live_state = "FLAT"

        return actions

    def sync_from_broker(
        self,
        has_long: bool,
        long_entry: float,
        has_short: bool,
        short_entry: float,
    ) -> None:
        if has_long:
            self._current_pos = 1.0
            self._entry_price = long_entry
            self._peak_price = long_entry
            self._live_state = "LONG"
        elif has_short:
            self._current_pos = -1.0
            self._entry_price = short_entry
            self._trough_price = short_entry
            self._live_state = "SHORT"
        else:
            self._current_pos = 0.0
            self._live_state = "FLAT"
