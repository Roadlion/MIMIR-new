# bong_strats/strategies/gold_math_scalper.py
"""
Gold Short-Term Mathematical Scalper (XAU/USD)
==============================================
Intraday quantitative strategy engineered specifically for Gold's high-volatility
microstructure, utilizing:
1. Ornstein-Uhlenbeck (OU) Mean-Reversion calibration for statistical mispricing.
2. Volatility regime classification (SQUEEZE vs EXPANDING vs EXHAUSTION).
3. Rigorous mathematical risk controls:
   - Dynamic ATR-based Stop Loss & Take Profit with asymmetric Risk:Reward (>= 1:2).
   - Dynamic break-even ratcheting & trailing ATR stops.
   - OU Half-Life Time-Decay Exit (cuts trades when the thesis takes too long to revert).
   - Inverse Volatility Parity sizing (downscales risk during massive volatility spikes).
"""

from __future__ import annotations

import math
from typing import Dict, Any, List
import numpy as np
import pandas as pd
from bong_strats.base_strategy import StaticStrategy
from bong_strats.indicators import OUCalibrator, VolatilityModel, compute_atr


class GoldMathScalperStrategy(StaticStrategy):
    name = "gold_math_scalper"
    description = (
        "Gold Short-Term Mathematical Scalper (OU mean-reversion, dynamic ATR risk "
        "management, half-life time decay exit, and volatility parity scaling)"
    )

    param_space = {
        # Long thresholds (Riding mean-reversion bounces in Gold)
        "long_zscore_entry": (1.4, 2.8),
        "long_stop_atr_mult": (0.8, 2.2),
        "long_tp_atr_mult": (1.8, 4.5),

        # Short thresholds (Tighter stops & fast profit taking on counter-trend fades)
        "short_zscore_entry": (1.6, 3.0),
        "short_stop_atr_mult": (0.6, 1.8),
        "short_tp_atr_mult": (1.2, 3.5),

        "zscore_exit": (0.05, 0.50),
        "max_half_life_mult": (1.0, 3.5),
        "trailing_activation_atr": (0.8, 2.0),
        "target_vol": (0.005, 0.025),
    }

    def __init__(
        self,
        # Long parameters
        long_zscore_entry: float = 1.80,
        long_stop_atr_mult: float = 1.20,
        long_tp_atr_mult: float = 2.40,

        # Short parameters
        short_zscore_entry: float = 2.00,
        short_stop_atr_mult: float = 0.90,
        short_tp_atr_mult: float = 1.80,

        zscore_exit: float = 0.20,
        max_half_life_mult: float = 2.0,
        trailing_activation_atr: float = 1.0,
        target_vol: float = 0.012,
        mode: str = "adaptive",
        # Backward compatibility aliases
        zscore_entry: Optional[float] = None,
        stop_atr_mult: Optional[float] = None,
        tp_atr_mult: Optional[float] = None,
        **kwargs: Any,
    ):
        self.long_zscore_entry = float(zscore_entry if zscore_entry is not None else long_zscore_entry)
        self.short_zscore_entry = float(zscore_entry if zscore_entry is not None else short_zscore_entry)

        self.long_stop_atr_mult = float(stop_atr_mult if stop_atr_mult is not None else long_stop_atr_mult)
        self.short_stop_atr_mult = float(stop_atr_mult if stop_atr_mult is not None else short_stop_atr_mult)

        self.long_tp_atr_mult = float(tp_atr_mult if tp_atr_mult is not None else long_tp_atr_mult)
        self.short_tp_atr_mult = float(tp_atr_mult if tp_atr_mult is not None else short_tp_atr_mult)

        self.zscore_exit = float(zscore_exit)
        self.max_half_life_mult = float(max_half_life_mult)
        self.trailing_activation_atr = float(trailing_activation_atr)
        self.target_vol = float(target_vol)
        self.mode = mode

        # Bear Market Defense & Macro Trend Controls
        self.enable_trend_defense: bool = True
        self.bear_short_zscore: float = 1.30      # Loosened Short threshold in Bear Trend (fade bounces)
        self.bear_long_zscore: float = 2.80       # Strict Long threshold in Bear Trend (no knife-catching)
        self.bull_long_zscore: float = 1.50       # Loosened Long threshold in Bull Trend
        self.bull_short_zscore: float = 2.80      # Strict Short threshold in Bull Trend
        self.cooldown_bars: int = 15              # Post-Stop Loss cooldown bars
        self._cooldown_remaining: int = 0
        self._last_stopped_dir: int = 0           # +1 for stopped long, -1 for stopped short

        # Internal State Machine
        self._state = "FLAT"
        self._pos = 0.0  # +1.0 Long, -1.0 Short, 0.0 Flat
        self._entry_price = 0.0
        self._stop_loss = 0.0
        self._take_profit = 0.0
        self._trailing_stop = 0.0
        self._bars_in_trade = 0
        self._est_half_life = 15.0
        self._current_size_scale = 1.0
        self._exit_reason = "NONE"

    @property
    def state(self) -> str:
        return self._state

    @property
    def net_position(self) -> float:
        return self._pos

    def tick(self, price: float, features: Dict[str, Any]) -> List[str]:
        """Live bar execution state machine."""
        actions: List[str] = []
        if price <= 0:
            return actions

        zscore = float(features.get("ou_zscore", 0.0))
        theta = max(float(features.get("ou_theta", 0.05)), 1e-6)
        ewma_vol = max(float(features.get("ewma_vol", 0.01)), 1e-4)
        regime = str(features.get("volatility_regime", "STABLE")).upper()
        rsi = float(features.get("rsi_14", 50.0))
        atr = float(features.get("atr_14", price * 0.003))
        if atr <= 0:
            atr = price * 0.003

        # Update half-life estimate: t_half = ln(2) / theta
        self._est_half_life = float(np.clip(math.log(2.0) / theta, 5.0, 100.0))

        # Dynamic Inverse Volatility Parity scaling factor
        self._current_size_scale = float(np.clip(self.target_vol / ewma_vol, 0.25, 2.0))

        # ---------------------------------------------------------------------
        # 1. Active Position Management & Mathematical Risk Exits
        # ---------------------------------------------------------------------
        if self._pos > 0.0:
            self._bars_in_trade += 1

            # (A) Trailing Stop & Break-Even Ratchet
            if price >= self._entry_price + (self.trailing_activation_atr * atr):
                be_level = self._entry_price + (0.2 * atr)
                dynamic_trail = price - (self.long_stop_atr_mult * atr)
                self._trailing_stop = max(self._trailing_stop, be_level, dynamic_trail)

            # (B) Mathematical Exit Triggers
            stop_lvl = max(self._stop_loss, self._trailing_stop)
            hit_stop = price <= stop_lvl
            hit_tp = price >= self._take_profit
            # Reverted to fair value equilibrium
            hit_mean_target = zscore >= -self.zscore_exit
            # Time Decay Stop: exceeded maximum OU half-life duration
            max_allowed_bars = int(self.max_half_life_mult * self._est_half_life)
            time_decay_expired = (self._bars_in_trade >= max_allowed_bars) and (price <= self._entry_price + 0.5 * atr)

            if hit_stop or hit_tp or hit_mean_target or time_decay_expired:
                if hit_stop:
                    self._exit_reason = "STOP_LOSS"
                    self._cooldown_remaining = self.cooldown_bars
                    self._last_stopped_dir = 1
                elif hit_tp:
                    self._exit_reason = "TAKE_PROFIT"
                elif hit_mean_target:
                    self._exit_reason = "MEAN_REVERTED"
                else:
                    self._exit_reason = "TIME_DECAY"

                self._pos = 0.0
                self._state = "FLAT"
                self._bars_in_trade = 0
                actions.append("CLOSE_LONG")
                return actions

        elif self._pos < 0.0:
            self._bars_in_trade += 1

            # (A) Trailing Stop & Break-Even Ratchet
            if price <= self._entry_price - (self.trailing_activation_atr * atr):
                be_level = self._entry_price - (0.2 * atr)
                dynamic_trail = price + (self.short_stop_atr_mult * atr)
                self._trailing_stop = min(self._trailing_stop, be_level, dynamic_trail) if self._trailing_stop > 0 else dynamic_trail

            # (B) Mathematical Exit Triggers
            stop_lvl = min(self._stop_loss, self._trailing_stop) if self._trailing_stop > 0 else self._stop_loss
            hit_stop = price >= stop_lvl
            hit_tp = price <= self._take_profit
            # Reverted to fair value equilibrium
            hit_mean_target = zscore <= self.zscore_exit
            # Time Decay Stop
            max_allowed_bars = int(self.max_half_life_mult * self._est_half_life)
            time_decay_expired = (self._bars_in_trade >= max_allowed_bars) and (price >= self._entry_price - 0.5 * atr)

            if hit_stop or hit_tp or hit_mean_target or time_decay_expired:
                if hit_stop:
                    self._exit_reason = "STOP_LOSS"
                    self._cooldown_remaining = self.cooldown_bars
                    self._last_stopped_dir = -1
                elif hit_tp:
                    self._exit_reason = "TAKE_PROFIT"
                elif hit_mean_target:
                    self._exit_reason = "MEAN_REVERTED"
                else:
                    self._exit_reason = "TIME_DECAY"

                self._pos = 0.0
                self._state = "FLAT"
                self._bars_in_trade = 0
                actions.append("CLOSE_SHORT")
                return actions

        # Decrement Stop-Loss Cooldown if flat
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1

        # ---------------------------------------------------------------------
        # 2. Entry Logic (when FLAT) with Bear/Bull Macro Regime Defense
        # ---------------------------------------------------------------------
        if self._pos == 0.0:
            is_breakout_danger = regime in ("EXPANDING", "EXHAUSTION")
            trend_regime = str(features.get("trend_regime", "NEUTRAL")).upper()

            # Dynamic Threshold Inversion based on Macro Trend
            if self.enable_trend_defense and trend_regime == "BEAR":
                # In Bear Trend:
                # 1. Longs are strictly gated against knife-catching (requires extreme capitulation)
                eff_long_z = self.bear_long_zscore
                eff_long_rsi = 25.0
                # 2. Shorts are loosened to harvest the trend on relief bounces
                eff_short_z = self.bear_short_zscore
                eff_short_rsi = 50.0
                eff_short_tp_mult = 3.0
            elif self.enable_trend_defense and trend_regime == "BULL":
                # In Bull Trend: Longs are aggressive, Shorts are strictly gated
                eff_long_z = self.bull_long_zscore
                eff_long_rsi = 50.0
                eff_short_z = self.bull_short_zscore
                eff_short_rsi = 75.0
                eff_short_tp_mult = self.short_tp_atr_mult
            else:
                # Neutral / Range: Standard balanced mean-reversion
                eff_long_z = self.long_zscore_entry
                eff_long_rsi = 40.0
                eff_short_z = self.short_zscore_entry
                eff_short_rsi = 60.0
                eff_short_tp_mult = self.short_tp_atr_mult

            # Cooldown Circuit Breaker: prevent immediate re-entry in stopped direction
            long_allowed = not (self._cooldown_remaining > 0 and self._last_stopped_dir == 1)
            short_allowed = not (self._cooldown_remaining > 0 and self._last_stopped_dir == -1)

            # Mathematical Oversold Entry (LONG)
            long_condition = (
                long_allowed
                and zscore <= -eff_long_z
                and rsi < eff_long_rsi
                and (not is_breakout_danger or zscore <= -3.0)
            )

            # Mathematical Overbought Entry (SHORT)
            short_condition = (
                short_allowed
                and zscore >= eff_short_z
                and rsi > eff_short_rsi
                and (not is_breakout_danger or zscore >= 2.2)
            )

            if long_condition:
                self._pos = 1.0
                self._state = "LONG"
                self._entry_price = price
                self._stop_loss = price - (self.long_stop_atr_mult * atr)
                self._take_profit = price + (self.long_tp_atr_mult * atr)
                self._trailing_stop = self._stop_loss
                self._bars_in_trade = 0
                self._exit_reason = "OPEN"
                actions.append("OPEN_LONG")

            elif short_condition:
                self._pos = -1.0
                self._state = "SHORT"
                self._entry_price = price
                self._stop_loss = price + (self.short_stop_atr_mult * atr)
                self._take_profit = price - (eff_short_tp_mult * atr)
                self._trailing_stop = self._stop_loss
                self._bars_in_trade = 0
                self._exit_reason = "OPEN"
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
            f"GoldMath | Pos={pos_str} | Scale={self._current_size_scale:.2f}x | "
            f"HalfLife={self._est_half_life:.1f}b | Bars={self._bars_in_trade} | "
            f"Entry={self._entry_price:.2f} | SL={self._stop_loss:.2f} | "
            f"TP={self._take_profit:.2f} | LastExit={self._exit_reason} | PnL={pnl_pct:+.2f}%"
        )

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        """
        Backtest replay generating pd.Series of positions (-1.0, 0.0, 1.0)
        calibrating Ornstein-Uhlenbeck and simulating mathematical risk exits.
        """
        n = len(data)
        signals = pd.Series(0.0, index=data.index)
        if n < 50:
            return signals

        close = data["close"]
        high = data["high"] if "high" in data.columns else close
        low = data["low"] if "low" in data.columns else close

        # 1. Compute ATR
        atr = compute_atr(data, period=14).bfill()

        # 2. Compute RSI
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14, min_periods=1).mean()
        loss = (-delta.clip(upper=0)).rolling(14, min_periods=1).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = (100 - (100 / (1 + rs))).fillna(50.0)

        # 3. Macro Trend Regimes (EMA50 vs EMA200)
        ema_50 = close.ewm(span=min(50, n), adjust=False).mean()
        ema_200 = close.ewm(span=min(150, max(20, n)), adjust=False).mean()

        # 4. Compute OU Z-Score & Theta if not present
        if "ou_zscore" in data.columns and "ou_theta" in data.columns:
            zscores = data["ou_zscore"].fillna(0.0).values
            thetas = data["ou_theta"].fillna(0.05).values
        else:
            log_close = np.log(close.where(close > 0, np.nan)).values
            calibrator = OUCalibrator()
            zscores = np.zeros(n)
            thetas = np.full(n, 0.05)
            lookback = 40
            for i in range(lookback, n):
                win = log_close[i - lookback : i + 1]
                th, mu, sg = calibrator.calibrate(win)
                thetas[i] = th
                zscores[i] = calibrator.zscore(log_close[i], mu, sg, th, sample_std=np.std(win))

        # 5. Simulate State Machine with Rigorous Risk Engine & Trend Defense
        pos = 0.0
        entry_price = 0.0
        stop_loss = 0.0
        take_profit = 0.0
        trailing_stop = 0.0
        bars_in_trade = 0
        cooldown_remaining = 0
        last_stopped_dir = 0

        for i in range(n):
            p = float(close.iloc[i])
            cur_atr = float(atr.iloc[i])
            cur_rsi = float(rsi.iloc[i])
            z = float(zscores[i])
            th = max(float(thetas[i]), 1e-6)
            est_half_life = max(math.log(2.0) / th, 5.0)
            max_bars = int(self.max_half_life_mult * est_half_life)

            # Classify macro trend regime
            cur_ema50 = float(ema_50.iloc[i])
            cur_ema200 = float(ema_200.iloc[i])
            if p < cur_ema50 and cur_ema50 <= cur_ema200 * 1.002:
                trend_regime = "BEAR"
            elif p > cur_ema50 and cur_ema50 >= cur_ema200 * 0.998:
                trend_regime = "BULL"
            else:
                trend_regime = "NEUTRAL"

            # Dynamic thresholds according to macro trend
            if self.enable_trend_defense and trend_regime == "BEAR":
                eff_long_z = self.bear_long_zscore
                eff_long_rsi = 25.0
                eff_short_z = self.bear_short_zscore
                eff_short_rsi = 50.0
                eff_short_tp_mult = 3.0
            elif self.enable_trend_defense and trend_regime == "BULL":
                eff_long_z = self.bull_long_zscore
                eff_long_rsi = 50.0
                eff_short_z = self.bull_short_zscore
                eff_short_rsi = 75.0
                eff_short_tp_mult = self.short_tp_atr_mult
            else:
                eff_long_z = self.long_zscore_entry
                eff_long_rsi = 40.0
                eff_short_z = self.short_zscore_entry
                eff_short_rsi = 60.0
                eff_short_tp_mult = self.short_tp_atr_mult

            if pos > 0.0:
                bars_in_trade += 1
                # Trailing stop
                if p >= entry_price + (self.trailing_activation_atr * cur_atr):
                    be_level = entry_price + (0.2 * cur_atr)
                    trailing_stop = max(trailing_stop, be_level, p - self.long_stop_atr_mult * cur_atr)

                stop_lvl = max(stop_loss, trailing_stop)
                hit_stop = p <= stop_lvl
                hit_tp = p >= take_profit
                hit_mean = z >= -self.zscore_exit
                hit_time = (bars_in_trade >= max_bars) and (p <= entry_price + 0.5 * cur_atr)

                if hit_stop or hit_tp or hit_mean or hit_time:
                    if hit_stop:
                        cooldown_remaining = self.cooldown_bars
                        last_stopped_dir = 1
                    pos = 0.0
                    entry_price = 0.0
                    bars_in_trade = 0

            elif pos < 0.0:
                bars_in_trade += 1
                # Trailing stop
                if p <= entry_price - (self.trailing_activation_atr * cur_atr):
                    be_level = entry_price - (0.2 * cur_atr)
                    dynamic_trail = p + self.short_stop_atr_mult * cur_atr
                    trailing_stop = min(trailing_stop, be_level, dynamic_trail) if trailing_stop > 0 else dynamic_trail

                stop_lvl = min(stop_loss, trailing_stop) if trailing_stop > 0 else stop_loss
                hit_stop = p >= stop_lvl
                hit_tp = p <= take_profit
                hit_mean = z <= self.zscore_exit
                hit_time = (bars_in_trade >= max_bars) and (p >= entry_price - 0.5 * cur_atr)

                if hit_stop or hit_tp or hit_mean or hit_time:
                    if hit_stop:
                        cooldown_remaining = self.cooldown_bars
                        last_stopped_dir = -1
                    pos = 0.0
                    entry_price = 0.0
                    bars_in_trade = 0

            # Decrement cooldown when flat
            if pos == 0.0 and cooldown_remaining > 0:
                cooldown_remaining -= 1

            # Entries
            if pos == 0.0:
                long_allowed = not (cooldown_remaining > 0 and last_stopped_dir == 1)
                short_allowed = not (cooldown_remaining > 0 and last_stopped_dir == -1)

                if long_allowed and z <= -eff_long_z and cur_rsi < eff_long_rsi:
                    pos = 1.0
                    entry_price = p
                    stop_loss = p - self.long_stop_atr_mult * cur_atr
                    take_profit = p + self.long_tp_atr_mult * cur_atr
                    trailing_stop = stop_loss
                    bars_in_trade = 0
                elif short_allowed and z >= eff_short_z and cur_rsi > eff_short_rsi:
                    pos = -1.0
                    entry_price = p
                    stop_loss = p + self.short_stop_atr_mult * cur_atr
                    take_profit = p - eff_short_tp_mult * cur_atr
                    trailing_stop = stop_loss
                    bars_in_trade = 0

            signals.iloc[i] = pos

        return signals
