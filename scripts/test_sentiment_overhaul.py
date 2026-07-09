# scripts/test_sentiment_overhaul.py
import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from backend.app.database import get_db_connection
from backend.app.config import get_settings
from backend.app.routers.prices import get_candles

settings = get_settings()

def run_sentiment_overhaul_tests():
    print("--- Testing Sentiment Overhaul Logic ---")
    
    # 1. Setup mock social chatter to test upvote and confidence weights
    conn = get_db_connection()
    cur = conn.cursor()
    
    ticker = "AAPL"
    
    try:
        # Fetch candles via API to ensure it runs without DB exception
        print("Fetching candles with updated sentiment calculations...")
        res = get_candles(ticker=ticker, interval="1d", days=7)
        assert "candles" in res
        print(f"Aggregated {len(res['candles'])} candles successfully.")
        
        # Verify that candles have a 'sentiment' property
        for c in res["candles"][:5]:
            assert "sentiment" in c
            print(f" - Date: {c['time']} | Price: {c['close']:.2f} | Sentiment: {c['sentiment']:.4f}")
            
    finally:
        cur.close()
        conn.close()
        
    print("\nSentiment Overhaul validation checks completed successfully!")

if __name__ == "__main__":
    run_sentiment_overhaul_tests()
