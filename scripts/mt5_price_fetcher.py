import MetaTrader5 as mt5
from datetime import datetime
import csv
import os
import time

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.app.database import get_db_connection
from backend.app.config import get_settings
from psycopg2.extras import execute_values

settings = get_settings()

print("[SUCCESS] Initialized DB dependencies.")

# --- 2. Connect to MT5 ---
if not mt5.initialize():
    print(f"[ERROR] MT5 initialization failed. Error code: {mt5.last_error()}")
    quit()
print("[SUCCESS] Connected to MT5 Terminal.")

def get_db_tickers():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        
        # Get from dynamic tickers
        cur.execute(f"SELECT DISTINCT ticker FROM {settings.mimir_schema}.mimir_dynamic_tickers WHERE ticker IS NOT NULL")
        dynamic = [r[0] for r in cur.fetchall()]
        
        # Get from sentiment impacts
        cur.execute(f"SELECT DISTINCT ticker FROM {settings.mimir_schema}.mimir_sentiment_impacts WHERE ticker IS NOT NULL")
        sentiment = [r[0] for r in cur.fetchall()]
        
        # Get from portfolio
        cur.execute(f"SELECT DISTINCT ticker FROM {settings.mimir_schema}.mimir_portfolio WHERE ticker IS NOT NULL")
        portfolio = [r[0] for r in cur.fetchall()]
        
        # Merge all unique
        all_tickers = list(set(dynamic + sentiment + portfolio))
        
        # Ensure some core ones just in case DB is empty
        core = [
            "NVDA", "AAPL", "GOOGL", "MSFT", "AMZN", 
            "AVGO", "META", "TSLA", "MU", "BRK.B", 
            "LLY", "JPM", "WMT", "AMD", "V", 
            "JNJ", "XOM", "INTC", "MA", "AMAT", 
            "CSCO", "LRCX", "ABBV", "CAT", "BAC", 
            "COST", "UNH", "GE", "ORCL", "CVX", 
            "MS", "KO", "PG", "HD", "GS", 
            "PLTR", "NFLX", "KLAC", "MRK", "DELL", 
            "PANW", "GEV", "TXN", "AXP", "LIN", 
            "ANET", "C", "CRWD", "IBM", "TMUS", "BTCUSD"
        ]
        return list(set(all_tickers + core))
    finally:
        conn.close()

# List of tickers to track (Dynamic from DB + Core)
symbols = get_db_tickers()
print(f"[INFO] Loaded {len(symbols)} distinct tickers from database.")

def resolve_broker_symbol(symbol: str) -> str:
    """Tries exact symbol name and broker variations (.US suffix, hash prefix, dot removal)."""
    candidates = [
        symbol,
        f"{symbol}.US",
        f"#{symbol}",
        symbol.replace(".", ""),
        f"{symbol.replace('.', '')}.US"
    ]
    if symbol == "BTCUSD":
        candidates.extend(["BTCUSD", "BTC"])
    
    for cand in candidates:
        if mt5.symbol_select(cand, True):
            return cand
    return None

# Verify symbols actively and resolve broker naming scheme
active_symbols = []
symbol_map = {}

for symbol in symbols:
    resolved = resolve_broker_symbol(symbol)
    if resolved:
        print(f"[SYMBOL OK] Active and tracking: {symbol} (Broker symbol: {resolved})")
        if resolved not in active_symbols:
            active_symbols.append(resolved)
        symbol_map[resolved] = symbol
    else:
        print(f"[SYMBOL ERROR] '{symbol}' not found! Check your broker's exact spelling (e.g., AAPL.US or #AAPL)")

if not active_symbols:
    print("[CRITICAL] No valid symbols to track. Stopping script.")
    mt5.shutdown()
    quit()

def fetch_and_log():
    log_time = datetime.now().strftime('%H:%M:%S')
    batch_rows = []
    
    for b_symbol in active_symbols:
        rates = mt5.copy_rates_from_pos(b_symbol, mt5.TIMEFRAME_M1, 0, 1)
        
        if rates is not None and len(rates) > 0:
            bar = rates[0]
            bar_time = datetime.fromtimestamp(int(bar['time'])).strftime('%Y-%m-%d %H:%M:%S')
            display_symbol = symbol_map.get(b_symbol, b_symbol)
            
            batch_rows.append([
                display_symbol,
                bar_time,
                float(bar['open']),
                float(bar['high']),
                float(bar['low']),
                float(bar['close']),
                int(bar['tick_volume'])
            ])
        else:
            print(f"[DATA ERROR] Could not get data for {b_symbol}. Error: {mt5.last_error()}")
            
    if batch_rows:
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            
            sql = f"""
            INSERT INTO {settings.mimir_schema}.mimir_hourly_ohlcv 
            (ticker, timestamp, open, high, low, close, volume)
            VALUES %s
            ON CONFLICT (ticker, timestamp) DO UPDATE 
            SET open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume,
                scraped_at = NOW();
            """
            
            execute_values(cur, sql, batch_rows)
            
            # Send notification for realtime SSE
            cur.execute("NOTIFY price_updates, 'new_prices';")
            
            conn.commit()
            cur.close()
            
            print(f"\n--- Logged Batch to DB at {log_time} ---")
            print(f"Inserted {len(batch_rows)} rows.")
            
        except Exception as e:
            print(f"[DB ERROR] {e}")
        finally:
            if 'conn' in locals() and conn:
                conn.close()

# --- 3. Run Instantly First ---
print("\n[STARTING] Pulling initial batch immediately...")
fetch_and_log()

print("\n[RUNNING] Initial pull done. Now entering 1-minute clock sync loop...")

try:
    while True:
        # Perfect 1-minute clock alignment loop
        current_time = time.time()
        sleep_time = 60 - (current_time % 60)
        time.sleep(sleep_time)
        
        # Trigger every turnaround minute
        fetch_and_log()

except KeyboardInterrupt:
    print("\n[STOPPING] Script terminated by user.")

finally:
    mt5.shutdown()
