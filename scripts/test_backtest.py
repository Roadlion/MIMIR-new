# scripts/test_backtest.py
import os
import sys
import pandas as pd

# Adjust path to import backend
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from backend.app.analytics.backtester import BacktestEngine
from backend.app.analytics.expression_parser import FormulaParser

def test_parser():
    print("--- Testing Formula Parser ---")
    # Create mock dataframes for testing
    dates = pd.to_datetime(['2025-01-01', '2025-01-02', '2025-01-03'])
    tickers = ['AAPL', 'MSFT', 'NVDA']
    
    close_df = pd.DataFrame([
        [150.0, 300.0, 400.0],
        [152.0, 298.0, 410.0],
        [155.0, 305.0, 420.0]
    ], index=dates, columns=tickers)
    
    sentiment_df = pd.DataFrame([
        [0.5, -0.2, 0.8],
        [0.6, -0.5, 0.4],
        [0.2, 0.8, -0.1]
    ], index=dates, columns=tickers)
    
    data = {
        'close': close_df,
        'sentiment': sentiment_df
    }
    
    parser = FormulaParser(data)
    
    # 1. Test basic field retrieval
    print("Testing field retrieval...")
    res_close = parser.evaluate("close")
    assert res_close.equals(close_df), "Close dataframe retrieval failed."
    
    # 2. Test mathematical operators
    print("Testing binary operations...")
    res_math = parser.evaluate("sentiment * 2")
    assert res_math.loc[dates[0], 'AAPL'] == 1.0, "Multiplication failed."
    
    # 3. Test cross-sectional rank
    print("Testing rank()...")
    res_rank = parser.evaluate("rank(sentiment)")
    # AAPL (0.5), MSFT (-0.2), NVDA (0.8) -> Ranks: MSFT=1st, AAPL=2nd, NVDA=3rd
    # Percentile ranks should be roughly: MSFT=0.33, AAPL=0.66, NVDA=1.0
    assert res_rank.loc[dates[0], 'MSFT'] < res_rank.loc[dates[0], 'AAPL'], "Rank sorting failed."
    
    # 4. Test time-series delay
    print("Testing delay()...")
    res_delay = parser.evaluate("delay(close, 1)")
    assert pd.isna(res_delay.iloc[0, 0]), "Delay first row should be NaN."
    assert res_delay.iloc[1, 0] == 150.0, "Delay value mismatch."
    
    print("[OK] Parser tests passed successfully!\n")

def test_engine_run():
    print("--- Testing Backtest Engine ---")
    start = "2025-06-01"
    end = "2026-06-30"
    
    try:
        engine = BacktestEngine(start_date=start, end_date=end, universe='core')
        print(f"Loading data for core universe between {start} and {end}...")
        engine.load_data()
        print("Data loaded successfully!")
        
        # Test executing a simple strategy: Long assets with positive sentiment
        formula = "neutralize(scale(sentiment))"
        print(f"Running backtest for formula: '{formula}'...")
        res = engine.run(
            formula=formula,
            holding_period=2,
            slippage_bps=5.0,
            style='long_short'
        )
        
        print("\n[OK] Backtest completed successfully!")
        print("Diagnostics Output:")
        for k, v in res["metrics"].items():
            print(f"   {k:<20}: {v}")
            
        print(f"   Chart points: {len(res['chart'])}")
        print(f"   Recent trades logged: {len(res['trades'])}")
        
        # Basic assertions
        assert "sharpe" in res["metrics"], "Sharpe ratio missing."
        assert len(res["chart"]) > 0, "Chart series empty."
        
    except Exception as e:
        print(f"[FAIL] Engine run failed: {e}")
        raise e

def main():
    try:
        test_parser()
        test_engine_run()
        print("\n[SUCCESS] All quantitative backend tests completed successfully!")
    except Exception as e:
        print(f"\n[ERROR] Testing suite failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
