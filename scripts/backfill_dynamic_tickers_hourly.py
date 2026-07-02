# scripts/backfill_dynamic_tickers_hourly.py
import os
import sys
import time
import random
import yfinance as yf
from datetime import datetime
from curl_cffi.requests import Session
from psycopg2.extras import execute_values

# Adjust path so we can import backend modules
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()

def get_tls_session():
    """Create a curl_cffi session impersonating Chrome to bypass yfinance request blocks."""
    sess = Session(impersonate="chrome")
    sess.verify = False
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9"
    })
    return sess

def get_tickers_to_backfill():
    """Query dynamic tickers that have fewer than 100 historical bars in the database."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Get all unique tickers from dynamic tickers mapping
        cur.execute(f"SELECT DISTINCT ticker FROM {settings.mimir_schema}.mimir_dynamic_tickers WHERE ticker IS NOT NULL")
        all_dynamic = [row[0].strip().upper() for row in cur.fetchall()]
        
        # Get count of hourly prices for these tickers
        cur.execute(f"""
            SELECT ticker, COUNT(*) 
            FROM {settings.mimir_schema}.mimir_hourly_ohlcv 
            GROUP BY ticker
        """)
        counts = {row[0].strip().upper(): row[1] for row in cur.fetchall()}
        
        to_backfill = []
        for ticker in all_dynamic:
            if not ticker or len(ticker) > 10:
                continue
            # Ignore index symbols that are already handled or standard benchmarks
            if ticker.startswith('0P') or '.F' in ticker or ticker.endswith('.F'):
                continue
            if not all(c.isalnum() or c in '^=.-' for c in ticker):
                continue
                
            count = counts.get(ticker, 0)
            if count < 100:
                to_backfill.append(ticker)
                
        return sorted(list(set(to_backfill)))
    except Exception as e:
        print(f"[BACKFILL] Error gathering tickers: {e}")
        return []
    finally:
        cur.close()
        conn.close()

def backfill_ticker(ticker_symbol: str, session: Session) -> bool:
    """Fetch 1 year of hourly price data for a ticker and save to DB."""
    ticker_symbol = ticker_symbol.strip().lstrip('$').upper()
    print(f"[BACKFILL] Starting {ticker_symbol}...")
    
    try:
        ticker_obj = yf.Ticker(ticker_symbol, session=session)
        # Fetch 365 days of 1-hour interval data (the maximum history allowed for 1h interval)
        df = ticker_obj.history(period="365d", interval="1h")
        
        if df is None or df.empty:
            print(f"[BACKFILL] [WARNING] No data returned for {ticker_symbol}")
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
            print(f"[BACKFILL] [WARNING] Empty records parsed for {ticker_symbol}")
            return False
            
        conn = get_db_connection()
        cur = conn.cursor()
        try:
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
            print(f"[BACKFILL] [OK] Cached {len(records)} hourly rows for {ticker_symbol}")
            return True
        except Exception as e:
            conn.rollback()
            print(f"[BACKFILL] [ERROR] DB insert failed for {ticker_symbol}: {e}")
            return False
        finally:
            cur.close()
            conn.close()
            
    except Exception as e:
        print(f"[BACKFILL] [ERROR] Fetch failed for {ticker_symbol}: {e}")
        return False

def main():
    print(f"[{datetime.now()}] Starting Dynamic Tickers Hourly Backfill Script...")
    
    tickers = get_tickers_to_backfill()
    print(f"[BACKFILL] Found {len(tickers)} dynamic tickers requiring price backfill.")
    
    if not tickers:
        print("[BACKFILL] All tickers are already fully backfilled. Nothing to do!")
        return

    # Allow limiting run in case user wants to test
    limit = None
    if len(sys.argv) > 1:
        try:
            limit = int(sys.argv[1])
            print(f"[BACKFILL] Running with user limit of {limit} tickers.")
            tickers = tickers[:limit]
        except ValueError:
            pass

    session = get_tls_session()
    success_count = 0
    fail_count = 0
    
    for i, ticker in enumerate(tickers):
        print(f"[BACKFILL] Processing ticker {i+1}/{len(tickers)}")
        success = backfill_ticker(ticker, session)
        if success:
            success_count += 1
        else:
            fail_count += 1
            
        # randomized delay to prevent rate limit (1.0 to 2.5 seconds)
        delay = random.uniform(1.0, 2.5)
        time.sleep(delay)
        
    print(f"[{datetime.now()}] Backfill completed! Successes: {success_count}, Failures: {fail_count}")

if __name__ == "__main__":
    main()
