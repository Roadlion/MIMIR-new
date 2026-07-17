# scripts/test_rsi_live.py
"""Quick sanity check: prints the live RSI for a handful of tickers."""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from backend.app.analytics.signal_fusion import get_recent_prices
from backend.app.analytics.technical_analysis import analyze_technical_indicators

TICKERS = ["AAPL", "NVDA", "TSLA", "MSFT", "AMD"]

for ticker in TICKERS:
    df = get_recent_prices(ticker)
    if df.empty or len(df) < 15:
        print(f"{ticker}: not enough data")
        continue
    ind = analyze_technical_indicators(df)
    print(f"{ticker:6s} | RSI: {ind['rsi']:6.2f} | Vol ratio: {ind['volume_ratio']:.2f}x | Trend: {ind['trend']}")
