# scripts/test_signal_fusion.py
import os
import sys
import pandas as pd
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from backend.app.analytics.signal_fusion import (
    get_recent_prices,
    get_recent_sentiment,
    scan_ticker_for_signals
)

def main():
    print("--- Testing Signal Fusion Engine ---")
    
    # Check if we can fetch prices for a default ticker
    test_ticker = "AAPL"
    print(f"Fetching recent daily prices for {test_ticker}...")
    df = get_recent_prices(test_ticker)
    print(f"Retrieved {len(df)} price records.")
    
    print(f"Fetching recent sentiment average for {test_ticker}...")
    sent = get_recent_sentiment(test_ticker)
    print(f"Sentiment: {sent}")
    
    print(f"Scanning ticker {test_ticker} for signals...")
    sig = scan_ticker_for_signals(test_ticker)
    if sig:
        print("Signal generated and saved:", sig)
    else:
        print("No signal triggered (or duplicate pending exists).")
        
    print("Signal fusion engine checks completed successfully.")

if __name__ == "__main__":
    main()
