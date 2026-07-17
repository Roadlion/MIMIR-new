# scripts/test_ml_signals.py
import os
import sys
import pandas as pd
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from backend.app.database import get_db_connection
from backend.app.analytics.signal_fusion import (
    get_daily_sentiment_history,
    get_xgb_prediction,
    scan_ticker_for_signals
)

def test_sentiment_history():
    print("Testing daily sentiment history extraction...")
    conn = get_db_connection()
    try:
        # Use a common ticker
        df = get_daily_sentiment_history("BTC-USD", days=30, conn=conn)
        assert isinstance(df, pd.DataFrame), "Result must be a pandas DataFrame"
        assert 'date' in df.columns, "DataFrame must contain 'date' column"
        assert 'sentiment' in df.columns, "DataFrame must contain 'sentiment' column"
        print(f"[SUCCESS] Sentiment history test passed. Rows fetched: {len(df)}")
    finally:
        conn.close()

def test_xgb_prediction_fallback():
    print("Testing XGBoost prediction function fallback...")
    # Create mock feature row matching Feature Columns exactly
    feature_cols = [
        'rsi', 'volume_ratio', 'close_to_support', 'close_to_resistance',
        'sentiment_1d', 'sentiment_3d', 'sentiment_5d',
        'price_momentum_5d', 'price_momentum_10d', 'volatility_20d',
        'pe_ratio', 'debt_to_equity', 'eps_growth', 'operating_margin'
    ]
    df_features = pd.DataFrame([[
        35.0, 1.2, 0.01, 0.05,
        0.4, 0.3, 0.2,
        -0.02, -0.05, 0.015,
        18.5, 80.0, 0.12, 0.22
    ]], columns=feature_cols)
    
    # Run prediction for a random dummy ticker
    # This should fallback to global models (if trained) or default 0.50
    prob_buy = get_xgb_prediction("AAPL", df_features, "buy")
    prob_sell = get_xgb_prediction("AAPL", df_features, "sell")
    
    assert 0.0 <= prob_buy <= 1.0, f"Probability must be in bounds [0, 1], got {prob_buy}"
    assert 0.0 <= prob_sell <= 1.0, f"Probability must be in bounds [0, 1], got {prob_sell}"
    print(f"[SUCCESS] XGBoost prediction test passed. BUY: {prob_buy:.3f}, SELL: {prob_sell:.3f}")

def test_scan_ticker():
    print("Testing scan_ticker_for_signals execution...")
    conn = get_db_connection()
    try:
        # Scan a common asset
        result = scan_ticker_for_signals("BTC-USD", conn=conn)
        # It's fine if it returns None (meaning no signal triggered), we just want to make sure it doesn't crash
        if result:
            print(f"[SUCCESS] Scan generated signal: {result['signal_type']} for {result['ticker']}")
        else:
            print("[SUCCESS] Scan executed successfully (no signal triggered).")
    finally:
        conn.close()

def run_all_tests():
    print("==================================================")
    print("Running Machine Learning Signals Validation Tests")
    print("==================================================")
    test_sentiment_history()
    test_xgb_prediction_fallback()
    test_scan_ticker()
    print("==================================================")
    print("All validation tests completed.")
    print("==================================================")

if __name__ == "__main__":
    run_all_tests()
