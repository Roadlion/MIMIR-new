# scripts/test_rsi_debug.py
import os
import sys
import pandas as pd
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from backend.app.analytics.signal_fusion import get_recent_prices
from backend.app.analytics.technical_analysis import calculate_rsi, analyze_technical_indicators

def main():
    ticker = "AAPL"
    df = get_recent_prices(ticker)
    print(f"Loaded {len(df)} rows for {ticker}")
    if df.empty:
        return
        
    print("Sample price data:")
    print(df.tail(10))
    
    close_prices = df["close"]
    delta = close_prices.diff()
    gain = delta.clip(lower=0)
    loss = -1 * delta.clip(upper=0)
    
    avg_gain = pd.Series(index=close_prices.index, dtype=float)
    avg_loss = pd.Series(index=close_prices.index, dtype=float)
    
    period = 14
    avg_gain.iloc[period] = gain.iloc[1:period+1].mean()
    avg_loss.iloc[period] = loss.iloc[1:period+1].mean()
    
    print("\nInitial avg_gain value at index 14:", avg_gain.iloc[14])
    print("Initial avg_loss value at index 14:", avg_loss.iloc[14])
    
    for i in range(period + 1, len(close_prices)):
        prev_gain = avg_gain.iloc[i - 1]
        curr_gain = gain.iloc[i]
        avg_gain.iloc[i] = (prev_gain * (period - 1) + curr_gain) / period
        
        prev_loss = avg_loss.iloc[i - 1]
        curr_loss = loss.iloc[i]
        avg_loss.iloc[i] = (prev_loss * (period - 1) + curr_loss) / period
        
        if i < 20:
            print(f"i: {i} | prev_gain: {prev_gain:.4f} | curr_gain: {curr_gain:.4f} | new_avg_gain: {avg_gain.iloc[i]:.4f}")
            
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi_vals = 100 - (100 / (1 + rs))
    rsi_vals = rsi_vals.fillna(50)
    
    print("\nCalculated RSI series (last 10 values):")
    for date, val in zip(rsi_vals.index[-10:], rsi_vals.iloc[-10:]):
        print(f"Date: {date} | RSI: {val}")
        
    indicators = analyze_technical_indicators(df)
    print("\nAnalyzed indicators:")
    print(indicators)

if __name__ == "__main__":
    main()
