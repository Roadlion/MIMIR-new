# scripts/verify_data_preparation.py
import os
import sys

# Adjust path to import database settings
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()

def run_query(conn, sql, description):
    cur = conn.cursor()
    try:
        cur.execute(sql)
        res = cur.fetchall()
        print(f"\n[OK] {description}")
        return res
    except Exception as e:
        conn.rollback()
        print(f"\n[FAIL] {description}: {e}")
        return None
    finally:
        cur.close()

def main():
    print("--- MIMIR Quant Data Verification Script ---")
    try:
        conn = get_db_connection()
    except Exception as e:
        print(f"Failed to connect to database: {e}")
        return

    # 1. Test Daily Price View
    print("\n1. Testing yggdrasil.v_mimir_daily_ohlcv view...")
    count_res = run_query(
        conn, 
        f"SELECT COUNT(*) FROM {settings.mimir_schema}.v_mimir_daily_ohlcv", 
        "Count total rows in daily view"
    )
    if count_res:
        print(f"   Total rows in daily view: {count_res[0][0]}")

    sample_res = run_query(
        conn,
        f"""
        SELECT ticker, COUNT(*), MIN(date), MAX(date)
        FROM {settings.mimir_schema}.v_mimir_daily_ohlcv
        GROUP BY ticker
        ORDER BY COUNT(*) DESC
        LIMIT 5
        """,
        "Fetch top 5 tickers in daily view"
    )
    if sample_res:
        print("   Sample daily tickers:")
        print(f"   {'Ticker':<10} | {'Rows':<6} | {'Start Date':<12} | {'End Date':<12}")
        print("   " + "-" * 48)
        for r in sample_res:
            print(f"   {r[0]:<10} | {r[1]:<6} | {str(r[2]):<12} | {str(r[3]):<12}")

    # 2. Check Dynamic Ticker Backfill Progress
    print("\n2. Checking dynamic ticker price coverage in mimir_hourly_ohlcv...")
    
    total_hourly = run_query(
        conn,
        f"SELECT COUNT(*), COUNT(DISTINCT ticker) FROM {settings.mimir_schema}.mimir_hourly_ohlcv",
        "Total hourly prices and distinct tickers"
    )
    if total_hourly:
        print(f"   Total hourly price rows: {total_hourly[0][0]}")
        print(f"   Total distinct tickers in DB: {total_hourly[0][1]}")

    dynamic_ticker_count = run_query(
        conn,
        f"SELECT COUNT(DISTINCT ticker) FROM {settings.mimir_schema}.mimir_dynamic_tickers WHERE ticker IS NOT NULL",
        "Count distinct tickers in taxonomy mapping"
    )
    
    covered_dynamic = run_query(
        conn,
        f"""
        SELECT COUNT(DISTINCT h.ticker) 
        FROM {settings.mimir_schema}.mimir_hourly_ohlcv h
        JOIN {settings.mimir_schema}.mimir_dynamic_tickers d ON h.ticker = d.ticker
        WHERE h.ticker IS NOT NULL
        """,
        "Count of dynamic tickers that have price records"
        
    )
    
    dense_dynamic = run_query(
        conn,
        f"""
        WITH t_counts AS (
            SELECT h.ticker, COUNT(*) as c
            FROM {settings.mimir_schema}.mimir_hourly_ohlcv h
            JOIN {settings.mimir_schema}.mimir_dynamic_tickers d ON h.ticker = d.ticker
            GROUP BY h.ticker
        )
        SELECT COUNT(*) FROM t_counts WHERE c >= 100
        """,
        "Count of dynamic tickers with dense price data (>= 100 bars)"
    )

    if dynamic_ticker_count and covered_dynamic and dense_dynamic:
        tot_dyn = dynamic_ticker_count[0][0]
        cov_dyn = covered_dynamic[0][0]
        dns_dyn = dense_dynamic[0][0]
        print(f"   Total distinct tickers in taxonomy mapping: {tot_dyn}")
        print(f"   Dynamic tickers with at least 1 price bar: {cov_dyn} ({round(cov_dyn/tot_dyn*100, 1)}%)")
        print(f"   Dynamic tickers with dense history (>=100 bars): {dns_dyn} ({round(dns_dyn/tot_dyn*100, 1)}%)")

    conn.close()
    print("\nVerification sequence finished.")

if __name__ == "__main__":
    main()
