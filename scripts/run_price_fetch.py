import os
import sys
import time
import threading
import yfinance as yf
from yfinance import cache as yf_cache
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from curl_cffi.requests import Session
from psycopg2.extras import execute_values

# Adjust path so we can import backend modules
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from backend.app.database import get_db_connection
from backend.app.config import get_settings
from backend.app.routers.prices import DEFAULT_TICKERS

settings = get_settings()

try:
    yf_cache.get_cookie_cache().dummy = True
    print("[BG_WORKER_PROC] cookie disk cache disabled (dummy=True)")
except Exception as e:
    print(f"[BG_WORKER_PROC] failed to disable cookie cache: {e}")

_tls = threading.local()

def _get_tls_session():
    if not hasattr(_tls, 'session'):
        sess = Session(impersonate="chrome")
        sess.verify = False
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9"
        })
        _tls.session = sess
    return _tls.session

def fetch_and_cache_minute_ticker(ticker_symbol: str, conn=None):
    """Fetches 1d of 1-minute interval history and caches it in SQL."""
    ticker_symbol = ticker_symbol.strip().lstrip('$').upper()
    try:
        session = _get_tls_session()
        ticker = yf.Ticker(ticker_symbol, session=session)
        df = ticker.history(period="1d", interval="1m")
        time.sleep(0.3)

        if df is None or df.empty:
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
            
        close_conn = False
        if conn is None:
            conn = get_db_connection()
            close_conn = True
        try:
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
        finally:
            if close_conn and conn:
                conn.close()
    except Exception as e:
        print(f"[BG_WORKER_PROC] Error caching minute OHLCV for {ticker_symbol}: {e}")
        return False

def run_price_fetch_cycle():
    """Gathers all active tickers and fetches their 1-minute prices concurrently."""
    print(f"[BG_WORKER_PROC] Starting 1-minute price fetch cycle at {datetime.now()}")
    
    tickers_to_fetch = list(DEFAULT_TICKERS)
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"""
            SELECT DISTINCT i.ticker 
            FROM {settings.mimir_schema}.mimir_sentiment_impacts i
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON i.article_id = a.id
            WHERE i.ticker IS NOT NULL
              AND a.published_ts >= NOW() - INTERVAL '12 hours'
        """)
        impact_tickers = [row[0] for row in cur.fetchall()]
        
        try:
            cur.execute(f"SELECT DISTINCT ticker FROM {settings.mimir_schema}.mimir_portfolio WHERE ticker IS NOT NULL")
            portfolio_tickers = [row[0] for row in cur.fetchall()]
        except Exception:
            portfolio_tickers = []
            
        cur.close()
        conn.close()
        
        combined = set(tickers_to_fetch + impact_tickers + portfolio_tickers)
        cleaned = []
        for t in combined:
            if not t:
                continue
            symbol = t.strip().lstrip('$').upper()
            if symbol.startswith('0P') or '.F' in symbol or symbol.endswith('.F'):
                continue
            if len(symbol) > 10:
                continue
            if not all(c.isalnum() or c in '^=.-' for c in symbol):
                continue
            cleaned.append(symbol)
        tickers_to_fetch = sorted(list(set(cleaned)))
    except Exception as e:
        print(f"[BG_WORKER_PROC] Error gathering tickers: {e}")
        
    if not tickers_to_fetch:
        return
        
    print(f"[BG_WORKER_PROC] Fetching 1-minute prices for {len(tickers_to_fetch)} tickers...")
    max_workers = min(len(tickers_to_fetch), 5)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        executor.map(lambda t: fetch_and_cache_minute_ticker(t, None), tickers_to_fetch)
        
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_minute_ohlcv WHERE timestamp < NOW() - INTERVAL '14 days'")
        conn.commit()
        cur.close()
        conn.close()
        print("[BG_WORKER_PROC] Cleaned up minute-level records older than 14 days.")
    except Exception as e:
        print(f"[BG_WORKER_PROC] Retention cleanup error: {e}")
        
    print(f"[BG_WORKER_PROC] 1-minute price fetch cycle completed.")

if __name__ == "__main__":
    run_price_fetch_cycle()
