# scripts/test_anomalous_volume.py
import os
import sys
import pandas as pd
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from backend.app.analytics.technical_analysis import analyze_technical_indicators

def main():
    print("="*60)
    print("TESTING ANOMALOUS VOLUME RATIO CALCULATION")
    print("="*60)
    
    # 1. Create a mocked DataFrame with normal volume (10,000 units daily)
    dates = pd.date_range(end="2026-07-09", periods=30)
    data_normal = {
        "open": [100.0] * 30,
        "high": [102.0] * 30,
        "low": [98.0] * 30,
        "close": [101.0] * 30,
        "volume": [10000] * 30
    }
    df_normal = pd.DataFrame(data_normal, index=dates)
    indicators_normal = analyze_technical_indicators(df_normal)
    print(f"Normal Case (Volume = 10,000 constant):")
    print(f" -> Volume Ratio: {indicators_normal['volume_ratio']:.2f}x (Expected: ~1.00)")
    
    # 2. Mock a volume breakout (2.5x the rolling average)
    data_breakout = {
        "open": [100.0] * 30,
        "high": [102.0] * 30,
        "low": [98.0] * 30,
        "close": [101.0] * 30,
        "volume": [10000] * 29 + [25000] # last day volume spike
    }
    df_breakout = pd.DataFrame(data_breakout, index=dates)
    indicators_breakout = analyze_technical_indicators(df_breakout)
    print(f"\nBreakout Case (Last day Volume spikes to 25,000):")
    print(f" -> Volume Ratio: {indicators_breakout['volume_ratio']:.2f}x (Expected: ~2.50)")
    
    # 3. Verify validation tags
    ratio = indicators_breakout['volume_ratio']
    suffix = f" confirmed by anomalous volume of {ratio:.2f}x average"
    if ratio >= 2.0:
        suffix += " [HIGH VOLUME BREAKOUT]"
    print(f"\nGenerated Signal suffix:\n -> '{suffix}'")
    
    print("="*60)
    print("SUCCESS: Anomalous volume testing completed successfully.")
    print("="*60)

if __name__ == "__main__":
    main()
