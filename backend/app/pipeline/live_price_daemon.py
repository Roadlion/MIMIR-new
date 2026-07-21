import os
import sys
import collections
from datetime import datetime, timedelta

# Fix path to load backend module
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)

import psycopg2
import psycopg2.extensions
import select
from backend.app.database import get_db_connection
from backend.app.config import get_settings
from backend.app.analytics.guerilla_hybrid import get_hybrid_signals

settings = get_settings()

# Python In-Memory sliding window cache (max 100 minutes per ticker)
# Using a deque provides O(1) append/pop efficiency, perfect for sliding time-series windows
price_cache = collections.defaultdict(lambda: collections.deque(maxlen=100))

def process_new_ticks():
    """Fetches the latest minute data, updates the sliding window cache, and triggers event-driven analytics."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        # Fetch the most recent minute tick for all tickers
        cur.execute(f"""
            SELECT ticker, timestamp, open, high, low, close, volume 
            FROM {settings.mimir_schema}.mimir_hourly_ohlcv
            WHERE timestamp >= NOW() - INTERVAL '2 minutes'
        """)
        rows = cur.fetchall()
        
        updated_tickers = []
        for row in rows:
            ticker, ts, o, h, l, c, v = row
            tick_data = {'timestamp': ts, 'open': float(o), 'high': float(h), 'low': float(l), 'close': float(c), 'volume': float(v)}
            
            # Avoid duplicate inserts into the sliding window
            if len(price_cache[ticker]) == 0 or price_cache[ticker][-1]['timestamp'] < ts:
                price_cache[ticker].append(tick_data)
                updated_tickers.append(ticker)
        
        if not updated_tickers:
            return
            
        print(f"[LIVE DAEMON] Updated sliding window cache for {len(updated_tickers)} tickers.")
        print(f"[LIVE DAEMON] Triggering event-driven analytics pipelines...")
        
        # 1. Trigger Event-Driven Statistical Arbitrage (Guerilla Quant)
        try:
            signals = get_hybrid_signals()
            if signals:
                print(f"[LIVE DAEMON] Evaluated {len(signals)} Stat-Arb pairs.")
        except Exception as e:
            print(f"[LIVE DAEMON] Error triggering Guerilla Hybrid: {e}")
            
        # 2. Trigger Event-Driven Technical Alerts
        try:
            from backend.app.routers.trade_alerts import evaluate_tick_technicals
            evaluate_tick_technicals(price_cache)
        except Exception as e:
            print(f"[LIVE DAEMON] Error triggering Technical Alerts: {e}")
            
        # 3. Trigger Event-Driven Sentiment Validation
        try:
            from backend.app.analytics.signal_fusion import validate_sentiment_with_price
            validate_sentiment_with_price(price_cache)
        except Exception as e:
            print(f"[LIVE DAEMON] Error triggering Sentiment Validation: {e}")
            
        # 4. Trigger Event-Driven Portfolio Stop-Losses
        try:
            from backend.app.routers.portfolio import evaluate_tick_stoploss
            evaluate_tick_stoploss(price_cache)
        except Exception as e:
            print(f"[LIVE DAEMON] Error triggering Portfolio Stop-Losses: {e}")
        
    finally:
        conn.close()

def listen_to_price_updates():
    """Listens to the PostgreSQL price_updates channel and fires process_new_ticks() instantly."""
    conn = get_db_connection()._conn
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute("LISTEN price_updates;")
    print("[LIVE DAEMON] Initialized Python In-Memory Sliding Window Cache (100min capacity).")
    print("[LIVE DAEMON] Listening for MT5 price_updates...")

    try:
        while True:
            # 5-second timeout polling
            if select.select([conn], [], [], 5) == ([], [], []):
                pass
            else:
                conn.poll()
                while conn.notifies:
                    conn.notifies.pop(0)
                    process_new_ticks()
    except Exception as e:
        print(f"[LIVE DAEMON ERROR] {e}")
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    listen_to_price_updates()
