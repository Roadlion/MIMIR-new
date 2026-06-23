# backend/app/routers/prices.py
from fastapi import APIRouter, Query, HTTPException
from typing import List, Optional
from datetime import datetime, timedelta, timezone
import yfinance as yf
import psycopg2
from psycopg2.extras import execute_values, RealDictCursor
from ..database import get_db_connection_dict, get_db_connection
from ..config import get_settings

router = APIRouter()
settings = get_settings()

DEFAULT_TICKERS = [
    "SPY", "QQQ", "^DJI", "^VIX", "GC=F", "CL=F", "DX-Y.NYB", 
    "EURUSD=X", "USDJPY=X", "AAPL", "MSFT", "NVDA", "TSLA"
]

import time
from curl_cffi.requests import Session

def fetch_and_cache_ticker(ticker_symbol: str, conn):
    """
    Fetches 7 days of hourly price history for the ticker using yfinance
    and inserts/caches it into the SQL database.
    """
    print(f"[YFINANCE] Starting fetch for {ticker_symbol}...")
    try:
        session = Session()
        session.verify = False
        ticker = yf.Ticker(ticker_symbol, session=session)
        # Fetch 7d hourly data
        df = ticker.history(period="7d", interval="1h")
        # Pause to avoid rate limits
        time.sleep(1.5)
        if df.empty:
            print(f"[YFINANCE] Received empty DataFrame for {ticker_symbol}")
            return False
        
        # Prepare data for bulk insert
        records = []
        for index, row in df.iterrows():
            # index is timezone-aware pandas Timestamp
            ts = index.to_pydatetime()
            price = float(row["Close"])
            volume = int(row["Volume"]) if "Volume" in row else 0
            records.append((ticker_symbol, ts, price, volume))
            
        if not records:
            print(f"[YFINANCE] No records parsed for {ticker_symbol}")
            return False
            
        cur = conn.cursor()
        # Insert or update
        sql = f"""
        INSERT INTO {settings.mimir_schema}.mimir_hourly_prices (ticker, timestamp, price, volume)
        VALUES %s
        ON CONFLICT (ticker, timestamp) DO UPDATE 
        SET price = EXCLUDED.price, volume = EXCLUDED.volume, scraped_at = NOW();
        """
        execute_values(cur, sql, records)
        conn.commit()
        cur.close()
        print(f"[YFINANCE] Successfully cached {len(records)} records for {ticker_symbol}")
        return True
    except Exception as e:
        print(f"[YFINANCE] Error fetching/caching {ticker_symbol}: {e}")
        return False

def get_ticker_price_data(ticker_symbol: str, conn):
    """
    Gets latest price and price ~24h ago from database.
    If database data is stale or missing, fetches new data from yfinance.
    """
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # 1. Check if we have recent data (less than 1 hour old)
    cur.execute(f"""
        SELECT MAX(timestamp) as max_ts
        FROM {settings.mimir_schema}.mimir_hourly_prices
        WHERE ticker = %s
    """, (ticker_symbol,))
    row = cur.fetchone()
    
    now = datetime.now(timezone.utc)
    need_fetch = True
    if row and row["max_ts"]:
        max_ts = row["max_ts"]
        if now - max_ts < timedelta(hours=1):
            need_fetch = False
            
    if need_fetch:
        print(f"Fetching fresh data for {ticker_symbol}...")
        fetch_and_cache_ticker(ticker_symbol, conn)
        
    # 2. Get latest price
    cur.execute(f"""
        SELECT price, timestamp
        FROM {settings.mimir_schema}.mimir_hourly_prices
        WHERE ticker = %s
        ORDER BY timestamp DESC
        LIMIT 1
    """, (ticker_symbol,))
    latest = cur.fetchone()
    
    if not latest:
        cur.close()
        return None
        
    latest_price = float(latest["price"])
    latest_ts = latest["timestamp"]
    
    # 3. Get price closest to 24h before the latest timestamp
    target_ts = latest_ts - timedelta(hours=24)
    cur.execute(f"""
        SELECT price, timestamp
        FROM {settings.mimir_schema}.mimir_hourly_prices
        WHERE ticker = %s AND timestamp <= %s
        ORDER BY timestamp DESC
        LIMIT 1
    """, (ticker_symbol, target_ts))
    prev = cur.fetchone()
    
    # If no price before 24h ago (e.g. data start), get the oldest available price
    if not prev:
        cur.execute(f"""
            SELECT price, timestamp
            FROM {settings.mimir_schema}.mimir_hourly_prices
            WHERE ticker = %s
            ORDER BY timestamp ASC
            LIMIT 1
        """, (ticker_symbol,))
        prev = cur.fetchone()
        
    cur.close()
    
    if not prev:
        return {
            "ticker": ticker_symbol,
            "current_price": latest_price,
            "price_24h_ago": latest_price,
            "change_percent": 0.0
        }
        
    prev_price = float(prev["price"])
    change_percent = 0.0
    if prev_price > 0:
        change_percent = ((latest_price - prev_price) / prev_price) * 100
        
    return {
        "ticker": ticker_symbol,
        "current_price": latest_price,
        "price_24h_ago": prev_price,
        "change_percent": round(change_percent, 2)
    }

@router.get("/prices/ticker-changes")
async def get_ticker_changes(tickers: Optional[str] = Query(None)):
    """
    Get current price and 24h change percentage for specified tickers.
    If no tickers are provided, returns default list.
    """
    ticker_list = DEFAULT_TICKERS
    if tickers:
        ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
        
    conn = get_db_connection()
    results = []
    for ticker in ticker_list:
        data = get_ticker_price_data(ticker, conn)
        if data:
            results.append(data)
            
    conn.close()
    return {"tickers": results}
