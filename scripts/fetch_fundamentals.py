# scripts/fetch_fundamentals.py
import os
import sys
import time
import urllib3
import yfinance as yf
from yfinance import cache as yf_cache
from datetime import datetime, timezone, timedelta
from pathlib import Path
from curl_cffi.requests import Session

# Adjust path so we can import backend modules
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()

try:
    yf_cache.get_cookie_cache().dummy = True
except Exception:
    pass

# Create a shared curl_cffi session with SSL verification disabled
session = Session(impersonate="chrome")
session.verify = False

def fetch_and_cache_fundamentals(ticker_symbol: str, cur, conn):
    ticker_symbol = ticker_symbol.strip().lstrip('$').upper()
    
    # Check if we already have fresh fundamentals (less than 1 day old)
    cur.execute(f"""
        SELECT pe_ratio, debt_to_equity, eps_growth, operating_margin, updated_at 
        FROM {settings.mimir_schema}.mimir_asset_fundamentals 
        WHERE ticker = %s
    """, (ticker_symbol,))
    row = cur.fetchone()
    
    if row:
        updated_at = row[4]
        if datetime.now(timezone.utc) - updated_at < timedelta(days=1):
            print(f"[FUNDAMENTALS] {ticker_symbol} is fresh (updated at {updated_at}). Skipping.")
            return "skipped"
            
    print(f"[FUNDAMENTALS] Fetching {ticker_symbol} from yfinance...")
    try:
        ticker = yf.Ticker(ticker_symbol, session=session)
        info = ticker.info
        if not info:
            print(f" [!] Empty info dictionary returned for {ticker_symbol}")
            return "failed"
            
        pe_ratio = info.get("trailingPE") or info.get("forwardPE")
        debt_to_equity = info.get("debtToEquity")
        eps_growth = info.get("earningsGrowth") or info.get("earningsQuarterlyGrowth")
        operating_margin = info.get("operatingMargins")
        
        # Convert types safely
        pe_ratio = float(pe_ratio) if pe_ratio is not None else None
        debt_to_equity = float(debt_to_equity) if debt_to_equity is not None else None
        eps_growth = float(eps_growth) if eps_growth is not None else None
        operating_margin = float(operating_margin) if operating_margin is not None else None
        
        cur.execute(f"""
            INSERT INTO {settings.mimir_schema}.mimir_asset_fundamentals (
                ticker, pe_ratio, debt_to_equity, eps_growth, operating_margin, updated_at
            ) VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (ticker) DO UPDATE 
            SET pe_ratio = EXCLUDED.pe_ratio,
                debt_to_equity = EXCLUDED.debt_to_equity,
                eps_growth = EXCLUDED.eps_growth,
                operating_margin = EXCLUDED.operating_margin,
                updated_at = NOW();
        """, (ticker_symbol, pe_ratio, debt_to_equity, eps_growth, operating_margin))
        conn.commit()
        print(f" [ok] {ticker_symbol} -> PE: {pe_ratio}, Debt/Eq: {debt_to_equity}, EPS Growth: {eps_growth}, Margin: {operating_margin}")
        return "fetched"
    except Exception as e:
        conn.rollback()
        print(f" [error] Failed to fetch fundamentals for {ticker_symbol}: {e}")
        return "failed"

def main():
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        # Load all active dynamic tickers from database
        cur.execute(f"SELECT DISTINCT ticker FROM {settings.mimir_schema}.mimir_dynamic_tickers WHERE ticker IS NOT NULL")
        tickers = [row[0].strip().upper() for row in cur.fetchall()]
        print(f"[FUNDAMENTALS] Loaded {len(tickers)} tickers for fundamentals check.")
        
        success_count = 0
        fetched_count = 0
        for ticker in tickers:
            status = fetch_and_cache_fundamentals(ticker, cur, conn)
            if status == "fetched":
                success_count += 1
                fetched_count += 1
                time.sleep(1.0)  # sleep only on actual network calls
            elif status == "skipped":
                success_count += 1
            else:  # failed
                time.sleep(0.5)
            
            # Limit actual network fetches to 15 per cycle to prevent rate limits
            if fetched_count >= 15:
                print("[FUNDAMENTALS] Cap of 15 network fetches reached. Remaining tickers will be updated in subsequent cycles.")
                break
                
        print(f"[FUNDAMENTALS] Successfully updated/checked {success_count} tickers. Fetched {fetched_count} from yfinance.")
    except Exception as e:
        print(f"[FUNDAMENTALS] Fatal error in cycle: {e}")
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    main()
