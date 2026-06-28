# backend/app/pipeline/background_worker.py
import os
import sys
import time
import asyncio
import subprocess
import threading
import yfinance as yf
from yfinance import cache as yf_cache
from datetime import datetime, timezone, timedelta

# Disable yfinance sqlite disk cookie cache to avoid persistent 401 crumb errors
try:
    yf_cache.get_cookie_cache().dummy = True
    print("[BG_WORKER] cookie disk cache disabled (dummy=True)")
except Exception as e:
    print(f"[BG_WORKER] failed to disable cookie cache: {e}")
from concurrent.futures import ThreadPoolExecutor
from curl_cffi.requests import Session
from psycopg2.extras import execute_values
from ..database import get_db_connection
from ..config import get_settings
from ..routers.prices import DEFAULT_TICKERS

settings = get_settings()

# Pathing for subprocess execution
ROUTER_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
PUSH_TO_DB_PATH = os.path.join(PROJECT_ROOT, "scripts", "push_to_db.py")
PIPELINE_PATH = os.path.join(PROJECT_ROOT, "scripts", "run_full_pipeline copy.py")

def fetch_and_cache_minute_ticker(ticker_symbol: str, conn=None):
    """Fetches 1d of 1-minute interval history and caches it in SQL."""
    ticker_symbol = ticker_symbol.strip().lstrip('$').upper()
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    try:
        session = Session(impersonate="chrome")
        session.verify = False
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9"
        })
        ticker = yf.Ticker(ticker_symbol, session=session)
        df = ticker.history(period="1d", interval="1m")
        time.sleep(0.3)
        
        if df.empty:
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
        return True
    except Exception as e:
        print(f"[BG_WORKER] Error caching minute OHLCV for {ticker_symbol}: {e}")
        return False
    finally:
        if close_conn and conn:
            conn.close()

def run_price_fetch_cycle():
    """Gathers all active tickers and fetches their 1-minute prices concurrently."""
    print(f"[BG_WORKER] Starting 1-minute price fetch cycle at {datetime.now()}")
    
    # Combine static and dynamic tickers
    tickers_to_fetch = list(DEFAULT_TICKERS)
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"SELECT DISTINCT ticker FROM {settings.mimir_schema}.mimir_sentiment_impacts WHERE ticker IS NOT NULL")
        impact_tickers = [row[0] for row in cur.fetchall()]
        cur.execute(f"SELECT DISTINCT ticker FROM {settings.mimir_schema}.mimir_dynamic_tickers WHERE ticker IS NOT NULL")
        dynamic_tickers = [row[0] for row in cur.fetchall()]
        cur.close()
        conn.close()
        
        combined = set(tickers_to_fetch + impact_tickers + dynamic_tickers)
        cleaned = [t.strip().lstrip('$').upper() for t in combined if t]
        tickers_to_fetch = sorted(list(set(cleaned)))
    except Exception as e:
        print(f"[BG_WORKER] Error gathering tickers: {e}")
        
    if not tickers_to_fetch:
        return
        
    print(f"[BG_WORKER] Fetching 1-minute prices for {len(tickers_to_fetch)} tickers...")
    max_workers = min(len(tickers_to_fetch), 5)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        executor.map(lambda t: fetch_and_cache_minute_ticker(t, None), tickers_to_fetch)
        
    # Manual retention cleanup (failsafe if TimescaleDB extension is missing)
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_minute_ohlcv WHERE timestamp < NOW() - INTERVAL '14 days'")
        conn.commit()
        cur.close()
        conn.close()
        print("[BG_WORKER] Cleaned up minute-level records older than 14 days.")
    except Exception as e:
        print(f"[BG_WORKER] Retention cleanup error: {e}")
        
    print(f"[BG_WORKER] 1-minute price fetch cycle completed.")

def run_news_and_sentiment_cycle():
    """Runs push_to_db.py followed by run_full_pipeline copy.py sequentially in a background thread."""
    print(f"[BG_WORKER] Starting breaking news & sentiment loop at {datetime.now()}")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    
    # 1. Scrape articles
    try:
        print("[BG_WORKER] Executing news scraper (push_to_db.py)...")
        res = subprocess.run([sys.executable, PUSH_TO_DB_PATH], env=env, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode != 0:
            print(f"[BG_WORKER] News scraper failed: {res.stderr}")
        else:
            print("[BG_WORKER] News scraping completed successfully.")
    except Exception as e:
        print(f"[BG_WORKER] Error running news scraper: {e}")
        
    # 2. Run sentiment pipeline
    try:
        print("[BG_WORKER] Executing sentiment pipeline (run_full_pipeline copy.py)...")
        res = subprocess.run([sys.executable, PIPELINE_PATH], env=env, cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode != 0:
            print(f"[BG_WORKER] Sentiment pipeline failed: {res.stderr}")
        else:
            print("[BG_WORKER] Sentiment pipeline completed successfully.")
    except Exception as e:
        print(f"[BG_WORKER] Error running sentiment pipeline: {e}")
        
    print(f"[BG_WORKER] Breaking news & sentiment loop completed.")

async def start_price_loop():
    """5-minute async loop for fetching 1-minute prices."""
    while True:
        try:
            # Run the block in a thread executor to avoid blocking the async event loop
            await asyncio.to_thread(run_price_fetch_cycle)
        except Exception as e:
            print(f"[BG_WORKER] Error in price loop: {e}")
        await asyncio.sleep(300) # every 5 minutes

async def start_news_loop():
    """5-minute async loop for fetching articles and analyzing sentiment."""
    while True:
        try:
            await asyncio.to_thread(run_news_and_sentiment_cycle)
        except Exception as e:
            print(f"[BG_WORKER] Error in news loop: {e}")
        await asyncio.sleep(300) # every 5 minutes

def start_background_worker():
    """Initializes and runs the background loops in daemon threads."""
    print("[BG_WORKER] Initializing MIMIR background workers...")
    
    def price_thread_worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(start_price_loop())
        
    def news_thread_worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(start_news_loop())
        
    t_price = threading.Thread(target=price_thread_worker, daemon=True)
    t_news = threading.Thread(target=news_thread_worker, daemon=True)
    
    t_price.start()
    t_news.start()
    print("[BG_WORKER] MIMIR background threads started successfully.")
