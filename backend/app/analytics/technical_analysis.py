# backend/app/analytics/technical_analysis.py
import numpy as np
import pandas as pd
from typing import Dict, List, Any, Tuple

def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """Calculates the Relative Strength Index (RSI) using Wilders smoothing."""
    if len(prices) < period + 1:
        return pd.Series(50.0, index=prices.index)
    
    delta = prices.diff()
    gain = delta.clip(lower=0)
    loss = -1 * delta.clip(upper=0)
    
    # Initialise the Wilders smoothed series
    avg_gain = pd.Series(np.nan, index=prices.index)
    avg_loss = pd.Series(np.nan, index=prices.index)
    
    # Seed: use the simple mean of the first `period` valid deltas (indices 1..period)
    # Index 0 of gain/loss is always NaN because prices.diff() returns NaN there.
    avg_gain.iloc[period] = gain.iloc[1:period + 1].mean()
    avg_loss.iloc[period] = loss.iloc[1:period + 1].mean()
    
    # Wilders smoothing from period+1 onwards
    for i in range(period + 1, len(prices)):
        avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gain.iloc[i]) / period
        avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (period - 1) + loss.iloc[i]) / period
        
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.fillna(50)  # Fallback to neutral if zero movement or insufficient data
    return rsi

def calculate_ma(prices: pd.Series, period: int, ma_type: str = "SMA") -> pd.Series:
    """Calculates Simple or Exponential Moving Averages."""
    if ma_type.upper() == "EMA":
        return prices.ewm(span=period, adjust=False, min_periods=period).mean()
    return prices.rolling(window=period, min_periods=period).mean()

def find_support_resistance(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 20) -> Tuple[float, float]:
    """
    Finds the nearest support and resistance levels relative to the current close price.
    Uses local extrema over a rolling window and aggregates them.
    Returns (support, resistance). If none found, returns close * 0.95 and close * 1.05.
    """
    if len(close) < window:
        return float(close.iloc[-1] * 0.95), float(close.iloc[-1] * 1.05)
        
    current_price = float(close.iloc[-1])
    
    # Find local minima/maxima in the window
    roll_min = low.rolling(window=5, center=True).min()
    roll_max = high.rolling(window=5, center=True).max()
    
    # Extrema are lines where rolling extremum equals the actual value
    minima = low[low == roll_min].dropna().tolist()
    maxima = high[high == roll_max].dropna().tolist()
    
    # Filter minima below current price (Support) and maxima above current price (Resistance)
    supports = [x for x in minima if x < current_price]
    resistances = [x for x in maxima if x > current_price]
    
    # Find closest support
    if supports:
        # Sort by proximity to current price
        supports.sort(key=lambda val: current_price - val)
        support = supports[0]
    else:
        support = current_price * 0.95
        
    # Find closest resistance
    if resistances:
        # Sort by proximity to current price
        resistances.sort(key=lambda val: val - current_price)
        resistance = resistances[0]
    else:
        resistance = current_price * 1.05
        
    return float(support), float(resistance)

def analyze_technical_indicators(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Performs full technical analysis on a ticker's historical DataFrame.
    Expects df with columns: ['open', 'high', 'low', 'close', 'volume'].
    Returns a dictionary of current indicators.
    """
    if df.empty or len(df) < 20:
        return {
            "rsi": 50.0,
            "ma_50": 0.0,
            "ma_200": 0.0,
            "support": 0.0,
            "resistance": 0.0,
            "trend": "NEUTRAL",
            "volume_ratio": 1.0
        }
        
    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"]
    
    # Indicators
    rsi_series = calculate_rsi(close)
    ma_50_series = calculate_ma(close, 50, "SMA")
    ma_200_series = calculate_ma(close, 200, "SMA")
    
    ma_vol_20_series = volume.rolling(window=20, min_periods=20).mean()
    
    current_rsi = float(rsi_series.iloc[-1])
    current_ma_50 = float(ma_50_series.iloc[-1]) if not np.isnan(ma_50_series.iloc[-1]) else float(close.iloc[-1])
    current_ma_200 = float(ma_200_series.iloc[-1]) if not np.isnan(ma_200_series.iloc[-1]) else float(close.iloc[-1])
    
    current_vol = float(volume.iloc[-1]) if not np.isnan(volume.iloc[-1]) else 0.0
    current_ma_vol_20 = float(ma_vol_20_series.iloc[-1]) if not np.isnan(ma_vol_20_series.iloc[-1]) else current_vol
    
    volume_ratio = current_vol / current_ma_vol_20 if current_ma_vol_20 > 0 else 1.0
    
    support, resistance = find_support_resistance(high, low, close)
    
    # Simple trend calculation
    trend = "NEUTRAL"
    current_price = float(close.iloc[-1])
    if current_price > current_ma_50 > current_ma_200:
        trend = "BULLISH"
    elif current_price < current_ma_50 < current_ma_200:
        trend = "BEARISH"
        
    return {
        "rsi": current_rsi,
        "ma_50": current_ma_50,
        "ma_200": current_ma_200,
        "support": support,
        "resistance": resistance,
        "trend": trend,
        "volume_ratio": volume_ratio
    }
