import sys
import os
import signal
import time
import csv
import math
from datetime import datetime
import MetaTrader5 as mt5

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.app.database import get_db_connection
from backend.app.config import get_settings
from backend.app.services.process_manager import record_pid
from psycopg2.extras import execute_values

# Record PID for clean shutdown management
record_pid("mt5_fetcher", os.getpid())

_running = True

def handle_signal(sig, frame):
    global _running
    _running = False
    print("\n[STOPPING] MT5 Price Fetcher received exit signal. Closing terminal connection...")
    try:
        mt5.shutdown()
    except Exception:
        pass
    sys.exit(0)

signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)
if hasattr(signal, 'SIGBREAK'):
    signal.signal(signal.SIGBREAK, handle_signal)

settings = get_settings()

print("[SUCCESS] Initialized DB dependencies.")

active_symbols = []
symbol_map = {}

def is_mt5_candidate(symbol: str) -> bool:
    """Filter out non-equity indices, currency crosses with =X, and foreign exchange tickers."""
    if not symbol or not isinstance(symbol, str):
        return False
    s = symbol.strip().upper()
    if s.startswith("^") or "=" in s or "/" in s:
        return False
    if "." in s and s != "BRK.B":
        return False
    if not s.replace(".", "").isalnum():
        return False
    return True

def get_db_tickers():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        
        # Get from dynamic tickers
        cur.execute(f"SELECT DISTINCT ticker FROM {settings.mimir_schema}.mimir_dynamic_tickers WHERE ticker IS NOT NULL")
        dynamic = [r[0] for r in cur.fetchall()]
        
        # Get from recent sentiment impacts (last 7 days)
        cur.execute(f"""
            SELECT DISTINCT ticker FROM {settings.mimir_schema}.mimir_sentiment_impacts 
            WHERE ticker IS NOT NULL AND created_at >= NOW() - INTERVAL '7 days'
        """)
        sentiment = [r[0] for r in cur.fetchall()]
        
        # Get from portfolio
        cur.execute(f"SELECT DISTINCT ticker FROM {settings.mimir_schema}.mimir_portfolio WHERE ticker IS NOT NULL")
        portfolio = [r[0] for r in cur.fetchall()]
        
        # Merge and filter valid candidates
        all_tickers = [tk.strip().upper() for tk in set(dynamic + sentiment + portfolio) if is_mt5_candidate(tk)]
        
        # Ensure core equities and crypto are present
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
        try:
            if mt5.symbol_select(cand, True):
                return cand
        except Exception:
            pass
    return None

def parse_bar_time(raw_time) -> str:
    """Safely parse MT5 bar timestamp into '%Y-%m-%d %H:%M:%S' string, guarding against Windows CRT OSError."""
    try:
        if raw_time is None:
            return None
        ts = float(raw_time)
        if not math.isfinite(ts):
            return None
        # Handle milliseconds/microseconds if returned
        if ts > 1e14:
            ts /= 1e6
        elif ts > 1e11:
            ts /= 1e3

        # Valid financial market timestamp must be reasonable (e.g. year 2000 to 2100)
        # Year 2000: 946684800, Year 2100: 4102444800
        if ts < 946684800 or ts > 4102444800:
            return None

        return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
    except (OSError, ValueError, OverflowError, TypeError):
        return None

def fetch_and_log():
    log_time = datetime.now().strftime('%H:%M:%S')
    batch_rows = []
    
    # Maximum signed 64-bit integer supported by PostgreSQL BIGINT
    MAX_BIGINT = 9223372036854775807
    
    for b_symbol in active_symbols:
        try:
            rates = mt5.copy_rates_from_pos(b_symbol, mt5.TIMEFRAME_M1, 0, 1)
            
            if rates is not None and len(rates) > 0:
                bar = rates[0]
                bar_time = parse_bar_time(bar['time'])
                if not bar_time:
                    # Skip corrupt or uninitialized bar timestamps (do not fabricate fake current time)
                    continue

                open_val = float(bar['open'])
                high_val = float(bar['high'])
                low_val = float(bar['low'])
                close_val = float(bar['close'])

                # Validate price finiteness and sanity
                if not (math.isfinite(open_val) and math.isfinite(high_val) and math.isfinite(low_val) and math.isfinite(close_val)):
                    continue
                if open_val <= 0.0 or high_val <= 0.0 or low_val <= 0.0 or close_val <= 0.0:
                    continue
                if high_val < low_val:
                    continue
                if open_val > 1e12 or high_val > 1e12 or low_val > 1e12 or close_val > 1e12:
                    continue

                # Guard against PostgreSQL BIGINT overflow (signed 64-bit max: 9,223,372,036,854,775,807)
                # MT5 returns np.uint64 which can be (ulong)-1 (18446744073709551615) or exceed 2^63 - 1
                vol = 0
                try:
                    raw_tv = int(bar['tick_volume'])
                    if raw_tv == 18446744073709551615 or raw_tv < 0:
                        vol = 0
                    elif raw_tv > MAX_BIGINT:
                        vol = MAX_BIGINT
                    else:
                        vol = raw_tv
                except (ValueError, TypeError, OverflowError):
                    vol = 0

                # If tick_volume is 0, check real_volume if available
                if vol == 0 and hasattr(bar, 'dtype') and 'real_volume' in bar.dtype.names:
                    try:
                        raw_rv = int(bar['real_volume'])
                        if 0 < raw_rv <= MAX_BIGINT and raw_rv != 18446744073709551615:
                            vol = raw_rv
                    except (ValueError, TypeError, OverflowError):
                        pass

                display_symbol = symbol_map.get(b_symbol, b_symbol)
                
                batch_rows.append([
                    display_symbol,
                    bar_time,
                    open_val,
                    high_val,
                    low_val,
                    close_val,
                    vol
                ])
            else:
                pass
        except Exception as sym_err:
            print(f"[DATA WARNING] Skipping {b_symbol} due to parse error: {sym_err}")
            
    if batch_rows:
        # Deduplicate and sort batch_rows by (ticker, timestamp) to enforce deterministic PostgreSQL lock ordering
        seen = set()
        deduped_rows = []
        for r in batch_rows:
            key = (r[0], r[1])
            if key not in seen:
                seen.add(key)
                deduped_rows.append(r)
        sorted_rows = sorted(deduped_rows, key=lambda x: (x[0], x[1]))

        max_retries = 3
        for attempt in range(max_retries):
            conn = None
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
                
                execute_values(cur, sql, sorted_rows)
                
                # Also log to mimir_minute_ohlcv
                min_sql = f"""
                INSERT INTO {settings.mimir_schema}.mimir_minute_ohlcv 
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
                execute_values(cur, min_sql, sorted_rows)

                # Upsert into mimir_latest_prices for instant sub-millisecond retrieval (deduped by ticker)
                latest_by_ticker = {}
                for r in sorted_rows:
                    latest_by_ticker[r[0]] = (r[0], r[5], r[5], 0.0, r[6], r[2], r[3], r[4], r[1])
                latest_rows = list(latest_by_ticker.values())

                latest_sql = f"""
                INSERT INTO {settings.mimir_schema}.mimir_latest_prices
                (ticker, latest_price, prev_close_24h, change_percent, volume, open, high, low, timestamp)
                VALUES %s
                ON CONFLICT (ticker) DO UPDATE SET
                    latest_price = EXCLUDED.latest_price,
                    change_percent = CASE 
                        WHEN mimir_latest_prices.prev_close_24h IS NOT NULL AND mimir_latest_prices.prev_close_24h > 0 
                        THEN ROUND(((EXCLUDED.latest_price - mimir_latest_prices.prev_close_24h) / mimir_latest_prices.prev_close_24h * 100)::numeric, 2)
                        ELSE mimir_latest_prices.change_percent
                    END,
                    volume = EXCLUDED.volume,
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    timestamp = EXCLUDED.timestamp,
                    updated_at = NOW();
                """
                execute_values(cur, latest_sql, latest_rows)
                
                # Send notification for realtime SSE
                cur.execute("NOTIFY price_updates, 'new_prices';")
                
                conn.commit()
                cur.close()
                conn.close()
                
                print(f"\n--- Logged Batch to DB at {log_time} ---")
                print(f"Inserted {len(sorted_rows)} rows.")
                break
                
            except Exception as e:
                if conn:
                    try:
                        conn.rollback()
                        conn.close()
                    except Exception:
                        pass
                if "deadlock" in str(e).lower() and attempt < max_retries - 1:
                    time.sleep(0.2 * (attempt + 1))
                    print(f"[DB RETRY] Deadlock detected, retrying batch insert (attempt {attempt + 2}/{max_retries})...")
                else:
                    print(f"[DB ERROR] {e}")
                    break

def main():
    # --- 2. Connect to MT5 ---
    if not mt5.initialize():
        print(f"[ERROR] MT5 initialization failed. Error code: {mt5.last_error()}")
        sys.exit(1)
    print("[SUCCESS] Connected to MT5 Terminal.")

    symbols = get_db_tickers()
    print(f"[INFO] Loaded {len(symbols)} distinct candidate tickers from database.")

    for symbol in symbols:
        resolved = resolve_broker_symbol(symbol)
        if resolved:
            print(f"[SYMBOL OK] Active and tracking: {symbol} (Broker symbol: {resolved})")
            if resolved not in active_symbols:
                active_symbols.append(resolved)
            symbol_map[resolved] = symbol

    if not active_symbols:
        print("[CRITICAL] No valid symbols to track. Stopping script.")
        mt5.shutdown()
        sys.exit(1)

    print(f"[INFO] Successfully resolved {len(active_symbols)} active broker symbols.")

    # --- 3. Run Instantly First ---
    print("\n[STARTING] Pulling initial batch immediately...")
    try:
        fetch_and_log()
    except Exception as init_err:
        print(f"[INIT PULL ERROR] {init_err}")

    print("\n[RUNNING] Initial pull done. Now entering 1-minute clock sync loop...")

    try:
        while _running:
            # Perfect 1-minute clock alignment loop with 0.5s check intervals
            current_time = time.time()
            sleep_time = 60 - (current_time % 60)
            target_time = current_time + sleep_time
            while time.time() < target_time and _running:
                time.sleep(0.5)

            if not _running:
                break

            # Trigger every turnaround minute
            try:
                fetch_and_log()
            except Exception as loop_err:
                print(f"[LOOP FETCH ERROR] {loop_err}")

    except (KeyboardInterrupt, SystemExit):
        print("\n[STOPPING] Script terminated.")
    finally:
        print("[MT5] Closing MetaTrader 5 session...")
        mt5.shutdown()

if __name__ == "__main__":
    main()
