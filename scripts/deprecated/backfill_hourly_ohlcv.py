# scripts/backfill_hourly_ohlcv.py
import os
import sys
import time
import yfinance as yf
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from curl_cffi.requests import Session
from psycopg2.extras import execute_values

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from backend.app.database import get_db_connection
from backend.app.config import get_settings
from backend.app.routers.prices import DEFAULT_TICKERS

settings = get_settings()

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {settings.mimir_schema}.mimir_hourly_ohlcv (
    ticker VARCHAR(20) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    open NUMERIC NOT NULL,
    high NUMERIC NOT NULL,
    low NUMERIC NOT NULL,
    close NUMERIC NOT NULL,
    volume BIGINT NOT NULL,
    scraped_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (ticker, timestamp)
);

CREATE INDEX IF NOT EXISTS idx_mimir_hourly_ohlcv_time ON {settings.mimir_schema}.mimir_hourly_ohlcv (timestamp DESC);
"""

def backfill_ticker_hourly(ticker_symbol: str):
    ticker_symbol = ticker_symbol.strip().lstrip('$').upper()
    print(f"[BACKFILL] Fetching 1-year (365d) hourly history for {ticker_symbol}...")
    conn = get_db_connection()
    try:
        session = Session(impersonate="chrome")
        session.verify = False
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9"
        })
        ticker = yf.Ticker(ticker_symbol, session=session)
        # Fetch 1 year of hourly data
        df = ticker.history(period="365d", interval="1h")
        time.sleep(0.5)
        
        if df.empty:
            print(f"[BACKFILL] No data returned for {ticker_symbol}")
            return False
            
        records = []
        for index, row in df.iterrows():
            ts = index.to_pydatetime()
            open_val = float(row["Open"])
            high_val = float(row["High"])
            low_val = float(row["Low"])
            close_val = float(row["Close"])
            volume_val = int(row["Volume"]) if "Volume" in row else 0
            records.append((ticker_symbol, ts, open_val, high_val, low_val, close_val, volume_val))
            
        if not records:
            return False
            
        cur = conn.cursor()
        
        # Table creation is done in main() beforehand
        
        sql = f"""
        INSERT INTO {settings.mimir_schema}.mimir_hourly_ohlcv (ticker, timestamp, open, high, low, close, volume)
        VALUES %s
        ON CONFLICT (ticker, timestamp) DO UPDATE 
        SET open = EXCLUDED.open,
            high = EXCLUDED.high,
            low = EXCLUDED.low,
            close = EXCLUDED.close,
            volume = EXCLUDED.volume,
            scraped_at = NOW();
        """
        execute_values(cur, sql, records)
        conn.commit()
        print(f"[BACKFILL] [OK] Backfilled {len(records)} hourly rows for {ticker_symbol}")
        cur.close()
        return True
    except Exception as e:
        print(f"[BACKFILL] [ERROR] Failed to backfill {ticker_symbol}: {e}")
        return False
    finally:
        conn.close()

def main():
    # 1. Fetch core MIMIR tickers
    tickers = list(DEFAULT_TICKERS)
    
    # 2. Add portfolio tickers
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"SELECT DISTINCT ticker FROM {settings.mimir_schema}.mimir_portfolio")
        portfolio_tickers = [row[0] for row in cur.fetchall()]
        for pt in portfolio_tickers:
            if pt not in tickers:
                tickers.append(pt)
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Could not read portfolio tickers: {e}")
        
    # Ensure table and schema exist before threading to prevent pg_class catalog lock deadlocks
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(CREATE_TABLE_SQL)
        conn.commit()
        cur.close()
        conn.close()
        print("[BACKFILL] Database table verified/created.")
    except Exception as e:
        print(f"[BACKFILL] Failed to create schema/table: {e}")
        return

    print(f"Starting 1-year hourly backfill for {len(tickers)} tickers: {tickers}...")
    
    # Run backfill concurrently
    with ThreadPoolExecutor(max_workers=5) as executor:
        executor.map(backfill_ticker_hourly, tickers)
        
    print("[BACKFILL] All tickers processed.")

if __name__ == "__main__":
    main()
