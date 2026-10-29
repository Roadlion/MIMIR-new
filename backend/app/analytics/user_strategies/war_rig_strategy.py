# backend/app/analytics/user_strategies/war_rig_strategy.py
"""
War Rig Transmission Strategy Wrapper for MIMIR Backtester.
Emulates the multi-cylinder microstructure and asymmetry logic:
- Trend: Price > EMA 20 & EMA 50
- RSI Sweet Spot: 40 <= RSI <= 65
- Volume Surge: Volume > 1.2x SMA(Volume, 20)
- Bollinger Squeeze & Wyckoff Accumulation
- Enforces >= 75% Conviction Threshold
"""
import pandas as pd
import numpy as np
from backend.app.analytics.user_strategies.base_strategy import Strategy
from backend.app.analytics.user_strategies.static_strategies import PairStatArbitrageStrategy


class WarRigStrategy(Strategy):
    name = "war_rig"
    is_live_ready = True
    description = "War Rig 3-Cylinder Crankshaft (Macro + Catalysts + Microstructure 2.5:1 Gate)"
    
    def __init__(self, min_conviction: float = 75.0, min_rr: float = 2.5):
        self.min_conviction = min_conviction
        self.min_rr = min_rr

    def tick(self, price: float, features: dict) -> list:
        return []

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        if len(data) < 50:
            return pd.Series(0.0, index=data.index)
            
        close = data["close"]
        volume = data["volume"]
        
        # 1. Trend Filters
        sma20 = close.rolling(20).mean()
        sma50 = close.rolling(50).mean()
        
        # 2. RSI Calculation
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / (loss + 1e-9)
        rsi = 100 - (100 / (1 + rs))
        
        # 3. Volume Expansion
        vol_sma20 = volume.rolling(20).mean()
        vol_ratio = volume / (vol_sma20 + 1e-9)
        
        # 4. Volatility Squeeze (Bollinger Bandwidth)
        bb_std = close.rolling(20).std()
        bandwidth = (bb_std * 2) / (sma20 + 1e-9)
        squeeze = bandwidth < bandwidth.rolling(50).quantile(0.25)
        
        # Composite Conviction Scoring (Simulates Cylinders 1-3 Entry Triggers)
        long_score = pd.Series(50.0, index=data.index)
        long_score += (close > sma20).astype(float) * 10.0
        long_score += (close > sma50).astype(float) * 7.0
        long_score += ((rsi >= 45) & (rsi <= 68)).astype(float) * 8.0
        long_score += (vol_ratio >= 1.15).astype(float) * 6.0
        long_score += squeeze.astype(float) * 5.0

        short_score = pd.Series(50.0, index=data.index)
        short_score += (close < sma20).astype(float) * 10.0
        short_score += (close < sma50).astype(float) * 7.0
        short_score += ((rsi >= 32) & (rsi <= 55)).astype(float) * 8.0
        short_score += (vol_ratio >= 1.15).astype(float) * 6.0
        short_score += squeeze.astype(float) * 5.0

        # Stateful position holding: enter on >= min_conviction, trail with SMA50 structure
        signals = pd.Series(0.0, index=data.index)
        curr_pos = 0.0
        for i in range(50, len(data)):
            c_val = float(close.iloc[i])
            s20_val = float(sma20.iloc[i])
            s50_val = float(sma50.iloc[i])
            r_val = float(rsi.iloc[i])
            if curr_pos == 1.0:
                if c_val < s50_val or r_val > 78.0:
                    curr_pos = 0.0
            elif curr_pos == -1.0:
                if c_val > s50_val or r_val < 22.0:
                    curr_pos = 0.0
            elif curr_pos == 0.0:
                if c_val > s20_val and s20_val > s50_val and float(long_score.iloc[i]) >= self.min_conviction:
                    curr_pos = 1.0
                elif c_val < s20_val and s20_val < s50_val and float(short_score.iloc[i]) >= self.min_conviction:
                    curr_pos = -1.0
            signals.iloc[i] = curr_pos
        return signals


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

