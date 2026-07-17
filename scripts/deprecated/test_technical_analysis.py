# scripts/test_technical_analysis.py
import os
import sys
import pandas as pd
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from backend.app.analytics.technical_analysis import (
    calculate_rsi,
    calculate_ma,
    find_support_resistance,
    analyze_technical_indicators
)

def run_tests():
    print("--- Testing Technical Analysis Engine ---")
    
    # Generate mock price series (e.g. 50 days)
    np.random.seed(42)
    dates = pd.date_range(start="2026-01-01", periods=100, freq="D")
    
    # Price trend: starts at 100, drifts up then down
    close_prices = []
    current_price = 100.0
    for i in range(100):
        drift = 0.5 if i < 60 else -0.5
        change = np.random.normal(drift, 1.0)
        current_price += change
        close_prices.append(current_price)
        
    df = pd.DataFrame({
        "open": [p - 0.5 for p in close_prices],
        "high": [p + 1.2 for p in close_prices],
        "low": [p - 1.2 for p in close_prices],
        "close": close_prices,
        "volume": np.random.randint(1000, 5000, size=100)
    }, index=dates)
    
    # 1. Test RSI
    print("Testing RSI...")
    rsi = calculate_rsi(df["close"], period=14)
    assert len(rsi) == 100
    assert not rsi.isna().all()
    # RSI must be between 0 and 100
    assert rsi.min() >= 0.0
    assert rsi.max() <= 100.0
    print(f"RSI calculated successfully. Current RSI: {rsi.iloc[-1]:.2f}")
    
    # 2. Test MA
    print("Testing SMA and EMA...")
    sma_20 = calculate_ma(df["close"], period=20, ma_type="SMA")
    ema_20 = calculate_ma(df["close"], period=20, ma_type="EMA")
    assert len(sma_20) == 100
    assert len(ema_20) == 100
    print("SMA/EMA calculated successfully.")
    
    # 3. Test Support & Resistance
    print("Testing Support and Resistance Levels...")
    sup, res = find_support_resistance(df["high"], df["low"], df["close"], window=20)
    print(f"Current Price: {df['close'].iloc[-1]:.2f} | Support: {sup:.2f} | Resistance: {res:.2f}")
    assert sup < df["close"].iloc[-1]
    assert res > df["close"].iloc[-1]
    
    # 4. Test Complete Indicator Analysis
    print("Testing analyze_technical_indicators...")
    analysis = analyze_technical_indicators(df)
    print("Analysis output:", analysis)
    assert "rsi" in analysis
    assert "support" in analysis
    assert "resistance" in analysis
    assert "trend" in analysis
    
    print("\nAll technical analysis engine tests passed successfully!")

if __name__ == "__main__":
    run_tests()
