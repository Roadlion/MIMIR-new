# scripts/test_backtest.py
import os
import sys
import pandas as pd
import numpy as np

# Adjust path to import backend
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from backend.app.analytics.backtester import BacktestEngine
from backend.app.analytics.expression_parser import FormulaParser
from backend.app.database import get_db_connection

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

def test_survivorship_and_portfolio_constraints():
    print("--- Testing Survivorship Bias & Portfolio Constraints ---")
    dates = pd.to_datetime(['2025-01-01', '2025-01-02', '2025-01-03'])
    tickers = ['AAPL', 'MSFT', 'NVDA']
    
    # AAPL is active all 3 days.
    # MSFT is only active on the first 2 days (delisted on day 3).
    # NVDA is only active on the last 2 days (listed on day 2).
    active_mask = pd.DataFrame([
        [True, True, False],   # Day 1: AAPL, MSFT active
        [True, True, True],    # Day 2: AAPL, MSFT, NVDA active
        [True, False, True]    # Day 3: AAPL, NVDA active
    ], index=dates, columns=tickers)
    
    close_df = pd.DataFrame([
        [100.0, 100.0, np.nan],
        [101.0, 102.0, 100.0],
        [102.0, np.nan, 101.0]
    ], index=dates, columns=tickers)
    
    # Fill close prices within active periods
    close_df = close_df.ffill().bfill().where(active_mask, np.nan)
    
    # Let's say sentiment is:
    sentiment_df = pd.DataFrame([
        [1.0, 2.0, 0.0], # Day 1: MSFT has higher sentiment than AAPL, NVDA is inactive
        [1.0, 2.0, 3.0], # Day 2: NVDA > MSFT > AAPL
        [2.0, 0.0, 1.0]  # Day 3: AAPL > NVDA, MSFT is inactive
    ], index=dates, columns=tickers)
    
    engine = BacktestEngine("2025-01-01", "2025-01-03")
    engine.active_mask = active_mask
    engine.dfs = {
        'close': close_df,
        'open': close_df,
        'high': close_df,
        'low': close_df,
        'volume': pd.DataFrame(1000.0, index=dates, columns=tickers),
        'returns': close_df.pct_change(fill_method=None).where(active_mask, np.nan).fillna(0.0),
        'sentiment': sentiment_df.where(active_mask, np.nan).fillna(0.0),
        'social_chatter': sentiment_df.where(active_mask, np.nan).fillna(0.0),
        'sentiment_spillover': sentiment_df.where(active_mask, np.nan).fillna(0.0)
    }
    
    # We will test long_short style with neutralization
    # Let's run a backtest with formula = "sentiment"
    res = engine.run(
        formula="sentiment",
        holding_period=1,
        slippage_bps=0.0,
        style="long_short"
    )
    
    trades = res["trades"]
    print("Generated Trades:")
    for t in trades:
        print(f"  {t['date']}: {t['ticker']} action={t['action']} weight={t['weight']}%")
        
    # Day 3 (2025-01-03) trades:
    day3_trades = [t for t in trades if t["date"] == "2025-01-03"]
    # Tickers traded on Day 3 should only be AAPL and NVDA. MSFT must not be traded!
    day3_tickers = [t["ticker"] for t in day3_trades]
    assert "MSFT" not in day3_tickers, "Delisted asset MSFT was traded on Day 3!"
    
    # Let's test portfolio_size constraint tie-breaking.
    # If portfolio_size = 1, on Day 2:
    # Active: AAPL (sentiment 1), MSFT (sentiment 2), NVDA (sentiment 3).
    # Longs: positive sentiment (AAPL, MSFT, NVDA). We select top 1: NVDA.
    res_p1 = engine.run(
        formula="sentiment",
        holding_period=1,
        slippage_bps=0.0,
        style="long_only",
        portfolio_size=1
    )
    
    # On Day 2: NVDA should be selected as the only long.
    trades_p1 = res_p1["trades"]
    day2_trades = [t for t in trades_p1 if t["date"] == "2025-01-02"]
    day2_tickers = [t["ticker"] for t in day2_trades]
    assert len(day2_tickers) <= 1, f"Portfolio size constraint violated: traded {day2_tickers}"
    assert "NVDA" in day2_tickers or len(day2_tickers) == 0, f"Expected NVDA to be selected, got {day2_tickers}"
    
    print("[OK] Survivorship bias & portfolio constraint tests passed successfully!\n")

def test_local_market_cutoffs():
    print("--- Testing Local Market Cutoff Times ---")
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # We test the SQL CASE logic for Korea (.KS) and US (default)
        query = """
            SELECT 
                -- Korea: 15:29 (before close)
                (CASE 
                    WHEN '000270.KS' LIKE '%.KS' THEN
                        CASE 
                            WHEN EXTRACT(HOUR FROM (TIMESTAMPTZ '2025-06-01 06:29:00 UTC' AT TIME ZONE 'Asia/Seoul')) * 60 + EXTRACT(MINUTE FROM (TIMESTAMPTZ '2025-06-01 06:29:00 UTC' AT TIME ZONE 'Asia/Seoul')) >= 930 THEN
                                ((TIMESTAMPTZ '2025-06-01 06:29:00 UTC' AT TIME ZONE 'Asia/Seoul') + INTERVAL '1 day')::date
                            ELSE
                                (TIMESTAMPTZ '2025-06-01 06:29:00 UTC' AT TIME ZONE 'Asia/Seoul')::date
                        END
                END) AS kr_before,
                
                -- Korea: 15:31 (after close)
                (CASE 
                    WHEN '000270.KS' LIKE '%.KS' THEN
                        CASE 
                            WHEN EXTRACT(HOUR FROM (TIMESTAMPTZ '2025-06-01 06:31:00 UTC' AT TIME ZONE 'Asia/Seoul')) * 60 + EXTRACT(MINUTE FROM (TIMESTAMPTZ '2025-06-01 06:31:00 UTC' AT TIME ZONE 'Asia/Seoul')) >= 930 THEN
                                ((TIMESTAMPTZ '2025-06-01 06:31:00 UTC' AT TIME ZONE 'Asia/Seoul') + INTERVAL '1 day')::date
                            ELSE
                                (TIMESTAMPTZ '2025-06-01 06:31:00 UTC' AT TIME ZONE 'Asia/Seoul')::date
                        END
                END) AS kr_after,

                -- US: 15:59 Eastern (before close)
                (CASE 
                    WHEN 'AAPL' LIKE '%.KS' THEN NULL
                    ELSE
                        CASE 
                            WHEN EXTRACT(HOUR FROM (TIMESTAMPTZ '2025-06-01 19:59:00 UTC' AT TIME ZONE 'America/New_York')) * 60 + EXTRACT(MINUTE FROM (TIMESTAMPTZ '2025-06-01 19:59:00 UTC' AT TIME ZONE 'America/New_York')) >= 960 THEN
                                ((TIMESTAMPTZ '2025-06-01 19:59:00 UTC' AT TIME ZONE 'America/New_York') + INTERVAL '1 day')::date
                            ELSE
                                (TIMESTAMPTZ '2025-06-01 19:59:00 UTC' AT TIME ZONE 'America/New_York')::date
                        END
                END) AS us_before,

                -- US: 16:01 Eastern (after close)
                (CASE 
                    WHEN 'AAPL' LIKE '%.KS' THEN NULL
                    ELSE
                        CASE 
                            WHEN EXTRACT(HOUR FROM (TIMESTAMPTZ '2025-06-01 20:01:00 UTC' AT TIME ZONE 'America/New_York')) * 60 + EXTRACT(MINUTE FROM (TIMESTAMPTZ '2025-06-01 20:01:00 UTC' AT TIME ZONE 'America/New_York')) >= 960 THEN
                                ((TIMESTAMPTZ '2025-06-01 20:01:00 UTC' AT TIME ZONE 'America/New_York') + INTERVAL '1 day')::date
                            ELSE
                                (TIMESTAMPTZ '2025-06-01 20:01:00 UTC' AT TIME ZONE 'America/New_York')::date
                        END
                END) AS us_after
        """
        cur.execute(query)
        row = cur.fetchone()
        kr_before, kr_after, us_before, us_after = row
        
        print(f"Korea before close (expected 2025-06-01): {kr_before}")
        print(f"Korea after close (expected 2025-06-02): {kr_after}")
        print(f"US before close (expected 2025-06-01): {us_before}")
        print(f"US after close (expected 2025-06-02): {us_after}")
        
        assert str(kr_before) == '2025-06-01', f"Korea before close failed: {kr_before}"
        assert str(kr_after) == '2025-06-02', f"Korea after close failed: {kr_after}"
        assert str(us_before) == '2025-06-01', f"US before close failed: {us_before}"
        assert str(us_after) == '2025-06-02', f"US after close failed: {us_after}"
        
        print("[OK] Local market timezone close cutoff tests passed successfully!\n")
    except Exception as e:
        print(f"[FAIL] Local market cutoff tests failed: {e}")
        raise e
    finally:
        cur.close()
        conn.close()

def main():
    try:
        test_parser()
        test_engine_run()
        test_survivorship_and_portfolio_constraints()
        test_local_market_cutoffs()
        print("\n[SUCCESS] All quantitative backend tests completed successfully!")
    except Exception as e:
        print(f"\n[ERROR] Testing suite failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
