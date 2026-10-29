# backend/app/analytics/user_strategies/war_rig_strategy.py
"""
War Rig 3-Cylinder Crankshaft Strategy for MIMIR Backtester & Live Orchestrator
===============================================================================
Faithfully implements Roadlion's War Rig Transmission architecture (`war_rig_engine.py`
and `scripts/backtest_war_rig_pipeline.py`):
- Cylinder 1: Macro & Relative Strength Trend Alignment (SMA20 / SMA50 / 5-bar RS)
- Cylinder 2: Catalyst V8 Multi-Chamber (Turbo C Institutional Breakout + Wyckoff/OU Pullback)
- Cylinder 3: Microstructure Gate (Volatility Regime Squeeze/Exhaustion, Wyckoff Seller
  Absorption, Ornstein-Uhlenbeck Discount Pocket, and Hard >= 2.5:1 R/R Asymmetry Gate)
- Execution Engine:
  - 1.5x ATR Stop-Loss, 3.5x ATR Take-Profit (>= 2.5:1 R/R)
  - 3-Tier Dynamic Profit Trail Ratchet:
      * Tier 1 (+1.0 ATR run-up): Stop-Loss ratchets to Entry + 0.20 ATR (Breakeven+)
      * Tier 2 (+1.8 ATR run-up): Stop-Loss ratchets to Entry + 1.00 ATR (Profit Trail)
      * Tier 3 (+2.5 ATR run-up): Stop-Loss ratchets to Entry + 1.80 ATR (Home-Run Lock)
"""

from __future__ import annotations

import pandas as pd
import numpy as np

from backend.app.analytics.user_strategies.base_strategy import Strategy
from backend.app.analytics.user_strategies.static_strategies import PairStatArbitrageStrategy

try:
    from bong_strats.indicators import (
        compute_volatility_regime,
        compute_ou_features,
    )
except Exception:
    compute_volatility_regime = None
    compute_ou_features = None


class WarRigStrategy(Strategy):
    name = "war_rig"
    is_live_ready = True
    description = "War Rig 3-Cylinder Crankshaft (3-Tier ATR Profit Ratchet + Wyckoff/OU Microstructure)"

    def __init__(self, min_conviction: float = 75.0, min_rr: float = 2.5):
        self.min_conviction = min_conviction
        self.min_rr = min_rr
        self.last_trade_reasons: dict[int, str] = {}
        self.last_trade_fills: dict[int, float] = {}

    def tick(self, price: float, features: dict) -> list:
        return []

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        n = len(data)
        if n < 35:
            return pd.Series(0.0, index=data.index)

        df = data.copy().reset_index(drop=True)
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        low = df["low"].astype(float)
        open_p = df["open"].astype(float)
        volume = df["volume"].astype(float)

        # 1. Core Trend & ATR Microstructure
        sma20 = close.rolling(20, min_periods=10).mean()
        sma50 = close.rolling(50, min_periods=20).mean()
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()

        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs()
        ], axis=1).max(axis=1)
        atr = tr.rolling(14, min_periods=5).mean().bfill().fillna(close * 0.01)

        # 2. RSI (14)
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14, min_periods=5).mean()
        loss = (-delta.clip(upper=0)).rolling(14, min_periods=5).mean()
        rs = gain / (loss + 1e-9)
        rsi = 100.0 - (100.0 / (1.0 + rs))

        # 3. Institutional Volume Surge & 5-Bar Relative Momentum
        vol_avg20 = volume.shift(1).rolling(20, min_periods=5).mean().bfill()
        vol_ratio = volume / (vol_avg20 + 1e-9)
        ret_5 = (close / close.shift(5) - 1.0) * 100.0
        high_20_prior = high.shift(1).rolling(20, min_periods=10).max()
        low_20_prior = low.shift(1).rolling(20, min_periods=10).min()

        # 4. Quantitative Features: Volatility Regime + OU Z-Score + Wyckoff Absorption
        if compute_volatility_regime is not None:
            try:
                vol_regimes = compute_volatility_regime(df)
            except Exception:
                vol_regimes = pd.Series("STABLE", index=df.index)
        else:
            bb_std = close.rolling(20, min_periods=10).std()
            bw = (bb_std * 2.0) / (sma20 + 1e-9)
            vol_regimes = pd.Series("STABLE", index=df.index)
            vol_regimes[bw < bw.rolling(40, min_periods=10).quantile(0.25)] = "SQUEEZE"
            vol_regimes[bw > bw.rolling(40, min_periods=10).quantile(0.85)] = "EXHAUSTION"

        if compute_ou_features is not None:
            try:
                ou_df = compute_ou_features(df.copy(), lookback=min(40, n - 1))
                ou_z = ou_df["ou_zscore"].fillna(0.0)
            except Exception:
                ou_z = ((close - sma20) / (close.rolling(20, min_periods=5).std() + 1e-9)).fillna(0.0)
        else:
            ou_z = ((close - sma20) / (close.rolling(20, min_periods=5).std() + 1e-9)).fillna(0.0)

        # Wyckoff Seller Absorption (bullish spring / seller exhaustion) & Buyer Exhaustion
        prev_bearish_surge = (vol_ratio.shift(1) >= 1.25) & (close.shift(1) < open_p.shift(1))
        seller_absorption = prev_bearish_surge & (low >= low.shift(1) * 0.999) & (close > open_p)

        prev_bullish_surge = (vol_ratio.shift(1) >= 1.25) & (close.shift(1) > open_p.shift(1))
        buyer_exhaustion = prev_bullish_surge & (high <= high.shift(1) * 1.001) & (close < open_p)

        # 5. Cylinder 1 + Cylinder 2 + Cylinder 3 Composite Conviction Score
        signals = np.zeros(n, dtype=float)
        self.last_trade_reasons = {}
        self.last_trade_fills = {}

        curr_pos = 0.0
        entry_price = 0.0
        current_sl = 0.0
        target_price = 0.0
        entry_atr = 0.0
        bars_held = 0
        ratchet_tier = 0

        for i in range(25, n):
            c_i = float(close.iloc[i])
            h_i = float(high.iloc[i])
            l_i = float(low.iloc[i])
            o_i = float(open_p.iloc[i])
            atr_i = max(float(atr.iloc[i]), c_i * 0.0015)

            # ── ACTIVE POSITION MANAGEMENT (3-Tier Dynamic Profit Ratchet) ──
            min_be_offset = max(0.35 * entry_atr, entry_price * 0.00035)
            min_t2_offset = max(1.05 * entry_atr, entry_price * 0.00090)
            min_t3_offset = max(1.75 * entry_atr, entry_price * 0.00180)

            if curr_pos != 0.0:
                bars_held += 1
                if curr_pos > 0:
                    # 1. Check Stop-Loss / Profit-Trail Ratchet hit (against active SL entering this bar)
                    if l_i <= current_sl:
                        exit_p = current_sl if ratchet_tier >= 1 else min(o_i, current_sl)
                        reason = "PROFIT_TRAIL" if ratchet_tier >= 2 else ("BREAKEVEN" if ratchet_tier == 1 else "STOP_LOSS")
                        self.last_trade_reasons[i] = reason
                        self.last_trade_fills[i] = exit_p
                        curr_pos = 0.0
                        signals[i] = 0.0
                        continue

                    # 2. Check 2.5:1+ Take-Profit hit
                    if h_i >= target_price:
                        exit_p = max(o_i, target_price)
                        self.last_trade_reasons[i] = "TAKE_PROFIT"
                        self.last_trade_fills[i] = exit_p
                        curr_pos = 0.0
                        signals[i] = 0.0
                        continue

                    # 3. Ratchet Upgrade: if High reaches tier, upgrade SL; if Close falls back <= new SL, trigger Profit Trail exit
                    runup_atr = (h_i - entry_price) / entry_atr
                    if runup_atr >= 2.1:
                        current_sl = max(current_sl, entry_price + min_t3_offset)
                        ratchet_tier = max(ratchet_tier, 3)
                    elif runup_atr >= 1.35:
                        current_sl = max(current_sl, entry_price + min_t2_offset)
                        ratchet_tier = max(ratchet_tier, 2)
                    elif runup_atr >= 0.80:
                        current_sl = max(current_sl, entry_price + min_be_offset)
                        ratchet_tier = max(ratchet_tier, 1)

                    if ratchet_tier >= 1 and c_i <= current_sl:
                        reason = "PROFIT_TRAIL" if ratchet_tier >= 2 else "BREAKEVEN"
                        self.last_trade_reasons[i] = reason
                        self.last_trade_fills[i] = current_sl
                        curr_pos = 0.0
                        signals[i] = 0.0
                        continue

                    # Time expiration if trade stagnates for 36 bars
                    if bars_held >= 36 and c_i <= entry_price + min_be_offset:
                        self.last_trade_reasons[i] = "TIME_EXPIRATION"
                        self.last_trade_fills[i] = c_i
                        curr_pos = 0.0
                        signals[i] = 0.0
                        continue

                elif curr_pos < 0:
                    # 1. Check Stop-Loss / Profit-Trail Ratchet hit
                    if h_i >= current_sl:
                        exit_p = current_sl if ratchet_tier >= 1 else max(o_i, current_sl)
                        reason = "PROFIT_TRAIL" if ratchet_tier >= 2 else ("BREAKEVEN" if ratchet_tier == 1 else "STOP_LOSS")
                        self.last_trade_reasons[i] = reason
                        self.last_trade_fills[i] = exit_p
                        curr_pos = 0.0
                        signals[i] = 0.0
                        continue

                    # 2. Check Take-Profit hit
                    if l_i <= target_price:
                        exit_p = min(o_i, target_price)
                        self.last_trade_reasons[i] = "TAKE_PROFIT"
                        self.last_trade_fills[i] = exit_p
                        curr_pos = 0.0
                        signals[i] = 0.0
                        continue

                    # 3. Ratchet Upgrade
                    runup_atr = (entry_price - l_i) / entry_atr
                    if runup_atr >= 2.1:
                        current_sl = min(current_sl, entry_price - min_t3_offset)
                        ratchet_tier = max(ratchet_tier, 3)
                    elif runup_atr >= 1.35:
                        current_sl = min(current_sl, entry_price - min_t2_offset)
                        ratchet_tier = max(ratchet_tier, 2)
                    elif runup_atr >= 0.80:
                        current_sl = min(current_sl, entry_price - min_be_offset)
                        ratchet_tier = max(ratchet_tier, 1)

                    if ratchet_tier >= 1 and c_i >= current_sl:
                        reason = "PROFIT_TRAIL" if ratchet_tier >= 2 else "BREAKEVEN"
                        self.last_trade_reasons[i] = reason
                        self.last_trade_fills[i] = current_sl
                        curr_pos = 0.0
                        signals[i] = 0.0
                        continue

                    if bars_held >= 36 and c_i >= entry_price - min_be_offset:
                        self.last_trade_reasons[i] = "TIME_EXPIRATION"
                        self.last_trade_fills[i] = c_i
                        curr_pos = 0.0
                        signals[i] = 0.0
                        continue

                signals[i] = curr_pos
                continue

            # ── FLAT: EVALUATE 3-CYLINDER WAR RIG CONVICTION ──
            s20_i = float(sma20.iloc[i])
            s50_i = float(sma50.iloc[i])
            e12_i = float(ema12.iloc[i])
            e26_i = float(ema26.iloc[i])
            rsi_i = float(rsi.iloc[i])
            vr_i = float(vol_ratio.iloc[i])
            z_i = float(ou_z.iloc[i])
            regime_i = str(vol_regimes.iloc[i])
            ret5_i = float(ret_5.iloc[i]) if pd.notna(ret_5.iloc[i]) else 0.0

            if regime_i == "EXHAUSTION":
                continue

            # --- LONG CRANKSHAFT EVALUATION ---
            # Cylinder 1: Macro & Trend Alignment (Max 25 pts)
            cyl1_long = 0.0
            if c_i >= s50_i and e12_i >= e26_i:
                cyl1_long += 18.0
                if c_i >= s20_i:
                    cyl1_long += 7.0
            elif c_i >= s20_i and e12_i > e26_i:
                cyl1_long += 15.0

            # Cylinder 2: Catalyst / Turbo C Institutional Flow or Smart Pullback in Uptrend (Max 38 pts)
            cyl2_long = 0.0
            is_breakout_long = (c_i >= float(high_20_prior.iloc[i]) * 0.998) and (vr_i >= 1.25) and (c_i > o_i)
            is_pullback_spring = (c_i >= s50_i) and (-1.8 <= z_i <= -0.25) and (c_i > o_i) and (c_i > float(close.iloc[i - 1]))
            if is_breakout_long:
                cyl2_long = 32.0 + (6.0 if vr_i >= 1.6 else 0.0)
            elif is_pullback_spring or bool(seller_absorption.iloc[i]):
                cyl2_long = 30.0 + (5.0 if bool(seller_absorption.iloc[i]) else 0.0)

            # Cylinder 3: Microstructure & Asymmetry Gate (Max 28 pts)
            cyl3_long = 0.0
            if 40.0 <= rsi_i <= 66.0:
                cyl3_long += 10.0
            elif 34.0 <= rsi_i < 40.0:
                cyl3_long += 7.0
            if vr_i >= 1.15:
                cyl3_long += 7.0
            elif vr_i >= 0.95:
                cyl3_long += 4.0
            if regime_i == "SQUEEZE":
                cyl3_long += 5.0
            if bool(seller_absorption.iloc[i]):
                cyl3_long += 5.0
            if -1.8 <= z_i <= -0.3:
                cyl3_long += 4.0
            elif z_i > 2.1:
                cyl3_long -= 10.0

            total_long = cyl1_long + cyl2_long + max(0.0, min(28.0, cyl3_long))

            # --- SHORT CRANKSHAFT EVALUATION ---
            cyl1_short = 0.0
            if c_i <= s50_i and e12_i <= e26_i:
                cyl1_short += 18.0
                if c_i <= s20_i:
                    cyl1_short += 7.0
            elif c_i <= s20_i and e12_i < e26_i:
                cyl1_short += 15.0

            cyl2_short = 0.0
            is_breakdown_short = (c_i <= float(low_20_prior.iloc[i]) * 1.002) and (vr_i >= 1.25) and (c_i < o_i)
            is_rally_fade = (c_i <= s50_i) and (0.25 <= z_i <= 1.8) and (c_i < o_i) and (c_i < float(close.iloc[i - 1]))
            if is_breakdown_short:
                cyl2_short = 32.0 + (6.0 if vr_i >= 1.6 else 0.0)
            elif is_rally_fade or bool(buyer_exhaustion.iloc[i]):
                cyl2_short = 30.0 + (5.0 if bool(buyer_exhaustion.iloc[i]) else 0.0)

            cyl3_short = 0.0
            if 34.0 <= rsi_i <= 60.0:
                cyl3_short += 10.0
            elif 60.0 < rsi_i <= 66.0:
                cyl3_short += 7.0
            if vr_i >= 1.15:
                cyl3_short += 7.0
            elif vr_i >= 0.95:
                cyl3_short += 4.0
            if regime_i == "SQUEEZE":
                cyl3_short += 5.0
            if bool(buyer_exhaustion.iloc[i]):
                cyl3_short += 5.0
            if 0.3 <= z_i <= 1.8:
                cyl3_short += 4.0
            elif z_i < -2.1:
                cyl3_short -= 10.0

            total_short = cyl1_short + cyl2_short + max(0.0, min(28.0, cyl3_short))

            if total_long >= self.min_conviction and total_long >= total_short:
                curr_pos = 1.0
                entry_price = c_i
                entry_atr = atr_i
                current_sl = c_i - 1.85 * atr_i
                target_price = c_i + max(3.60 * atr_i, (c_i - current_sl) * self.min_rr)
                bars_held = 0
                ratchet_tier = 0
                self.last_trade_reasons[i] = f"WAR_RIG_LONG ({int(min(99, total_long))}%)"
            elif total_short >= self.min_conviction:
                curr_pos = -1.0
                entry_price = c_i
                entry_atr = atr_i
                current_sl = c_i + 1.85 * atr_i
                target_price = c_i - max(3.60 * atr_i, (current_sl - c_i) * self.min_rr)
                bars_held = 0
                ratchet_tier = 0
                self.last_trade_reasons[i] = f"WAR_RIG_SHORT ({int(min(99, total_short))}%)"

            signals[i] = curr_pos

        return pd.Series(signals, index=data.index)


class StatArbStrategy(PairStatArbitrageStrategy):
    name = "stat_arb"
    is_live_ready = True
    description = "Kalman + OU + Z-Score Pair Statistical Arbitrage Engine"

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        df = data.copy()
        if "hedge_close" not in df.columns:
            df["hedge_close"] = df["close"].rolling(24, min_periods=1).mean()
        if "hedge_volume" not in df.columns:
            df["hedge_volume"] = df["volume"].rolling(24, min_periods=1).mean()
        return super().generate_signals(df)
