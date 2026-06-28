# scripts/backfill_minute_prices.py
import os
import sys
import time
import yfinance as yf
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from curl_cffi.requests import Session
from psycopg2.extras import execute_values

# Add project root to python path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from backend.app.database import get_db_connection
from backend.app.config import get_settings
from backend.app.routers.prices import DEFAULT_TICKERS

settings = get_settings()

def backfill_ticker_minute(ticker_symbol: str):
    ticker_symbol = ticker_symbol.strip().lstrip('$').upper()
    print(f"[BACKFILL] Fetching maximum 7-day 1-minute history for {ticker_symbol}...")
    conn = get_db_connection()
    try:
        session = Session(impersonate="chrome")
        session.verify = False
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9"
        })
        ticker = yf.Ticker(ticker_symbol, session=session)
        # Fetch 7d of 1m data (the maximum history allowed for 1m interval)
        df = ticker.history(period="7d", interval="1m")
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
        sql = f"""
        INSERT INTO {settings.mimir_schema}.mimir_minute_ohlcv (ticker, timestamp, open, high, low, close, volume)
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
        cur.close()
        print(f"[BACKFILL] Successfully cached {len(records)} 1-minute records for {ticker_symbol}")
        return True
    except Exception as e:
        print(f"[BACKFILL] Error backfilling {ticker_symbol}: {e}")
        return False
    finally:
        conn.close()

def main():
    print(f"[BACKFILL] Starting 1-minute historical backfill cycle at {datetime.now()}...")
    
    tickers = list(DEFAULT_TICKERS)
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT ticker FROM yggdrasil.mimir_sentiment_impacts WHERE ticker IS NOT NULL")
        impact_tickers = [row[0] for row in cur.fetchall()]
        cur.execute("SELECT DISTINCT ticker FROM yggdrasil.mimir_dynamic_tickers WHERE ticker IS NOT NULL")
        dynamic_tickers = [row[0] for row in cur.fetchall()]
        cur.close()
        conn.close()
        
        combined = set(tickers + impact_tickers + dynamic_tickers)
        tickers = sorted([t.strip().lstrip('$').upper() for t in combined if t])
    except Exception as e:
        print(f"[BACKFILL] Error gathering tickers: {e}")
        
    print(f"[BACKFILL] Total tickers to backfill: {len(tickers)}")
    
    # Run backfill concurrently
    max_workers = min(len(tickers), 5)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        executor.map(backfill_ticker_minute, tickers)
        
    print("[BACKFILL] Historical 1-minute price backfill cycle completed successfully!")

if __name__ == "__main__":
    main()
