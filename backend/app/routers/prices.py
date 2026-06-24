# backend/app/routers/prices.py
from fastapi import APIRouter, Query, HTTPException
from typing import List, Optional
from datetime import datetime, timedelta, timezone
import yfinance as yf
import psycopg2
import time
from curl_cffi.requests import Session
from psycopg2.extras import execute_values, RealDictCursor
from ..database import get_db_connection_dict, get_db_connection
from ..config import get_settings

router = APIRouter()
settings = get_settings()

DEFAULT_TICKERS = [
    "SPY", "QQQ", "^DJI", "^VIX", "GC=F", "CL=F", "DX-Y.NYB", 
    "EURUSD=X", "USDJPY=X", "AAPL", "MSFT", "NVDA", "TSLA", "^SET50.BK"
]

def fetch_and_cache_ticker(ticker_symbol: str, conn):
    """
    Fetches 7 days of hourly price history (OHLCV) for the ticker using yfinance
    and inserts/caches it into the SQL database.
    """
    print(f"[YFINANCE] Starting fetch for {ticker_symbol}...")
    try:
        session = Session(impersonate="chrome")
        session.verify = False
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9"
        })
        ticker = yf.Ticker(ticker_symbol, session=session)
        # Fetch 7d hourly data
        df = ticker.history(period="7d", interval="1h")
        # Pause to avoid rate limits
        time.sleep(1.5)
        
        if df.empty:
            print(f"[YFINANCE] Received empty DataFrame for {ticker_symbol}")
            return False
        
        # Prepare data for bulk insert (Open, High, Low, Close, Volume)
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
            print(f"[YFINANCE] No records parsed for {ticker_symbol}")
            return False
            
        cur = conn.cursor()
        # Insert or update
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
        cur.close()
        print(f"[YFINANCE] Successfully cached {len(records)} OHLCV records for {ticker_symbol}")
        return True
    except Exception as e:
        print(f"[YFINANCE] Error fetching/caching {ticker_symbol}: {e}")
        return False

def get_ticker_price_data(ticker_symbol: str, conn):
    """
    Gets latest close price and close price ~24h ago from database.
    If database data is missing, fetches new data from yfinance.
    """
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # 1. Check when it was last scraped
    cur.execute(f"""
        SELECT MAX(scraped_at) as last_scraped FROM {settings.mimir_schema}.mimir_hourly_ohlcv 
        WHERE ticker = %s
    """, (ticker_symbol,))
    row = cur.fetchone()
    last_scraped = row["last_scraped"] if row else None
    
    is_stale = False
    if not last_scraped:
        is_stale = True
    else:
        now_ts = datetime.now(timezone.utc)
        if now_ts - last_scraped > timedelta(hours=1):
            is_stale = True
            
    if is_stale:
        reason = "No data found" if not last_scraped else f"stale data (last scraped {last_scraped})"
        print(f"[DATABASE] Cache miss/stale for {ticker_symbol} ({reason}). Fetching from yfinance...")
        fetch_and_cache_ticker(ticker_symbol, conn)
    else:
        print(f"[DATABASE] Cache hit: Found recent cached price data for {ticker_symbol} (last scraped {last_scraped}). Skipping yfinance API call.")
        
    # 2. Get latest close price
    cur.execute(f"""
        SELECT close, timestamp
        FROM {settings.mimir_schema}.mimir_hourly_ohlcv
        WHERE ticker = %s
        ORDER BY timestamp DESC
        LIMIT 1
    """, (ticker_symbol,))
    latest = cur.fetchone()
    
    if not latest:
        cur.close()
        return None
        
    latest_price = float(latest["close"])
    latest_ts = latest["timestamp"]
    
    # 3. Get close price closest to 24h before the latest timestamp
    target_ts = latest_ts - timedelta(hours=24)
    cur.execute(f"""
        SELECT close, timestamp
        FROM {settings.mimir_schema}.mimir_hourly_ohlcv
        WHERE ticker = %s AND timestamp <= %s
        ORDER BY timestamp DESC
        LIMIT 1
    """, (ticker_symbol, target_ts))
    prev = cur.fetchone()
    
    # If no price before 24h ago (e.g. data start), get the oldest available price
    if not prev:
        cur.execute(f"""
            SELECT close, timestamp
            FROM {settings.mimir_schema}.mimir_hourly_ohlcv
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
        
    prev_price = float(prev["close"])
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

@router.get("/prices/candles")
async def get_candles(
    ticker: str,
    interval: str = Query("1h", pattern="^(1h|4h|1d)$"),
    days: int = Query(7, ge=1, le=30)
):
    """
    Get aggregated candlestick (OHLCV) data for a given ticker and time interval.
    Supported intervals: '1h' (hourly), '4h' (4-hourly), '1d' (daily).
    """
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # Ensure some data exists in cache for the ticker
    cur.execute(f"""
        SELECT EXISTS(
            SELECT 1 FROM {settings.mimir_schema}.mimir_hourly_ohlcv 
            WHERE ticker = %s
        )
    """, (ticker,))
    has_data = cur.fetchone()["exists"]
    
    if not has_data:
        fetch_and_cache_ticker(ticker, conn)
        
    start_time = datetime.now(timezone.utc) - timedelta(days=days)
    
    # Select aggregation query based on requested interval
    if interval == "1h":
        sql = f"""
            SELECT 
                timestamp AS time,
                open, high, low, close, volume
            FROM {settings.mimir_schema}.mimir_hourly_ohlcv
            WHERE ticker = %s AND timestamp >= %s
            ORDER BY timestamp ASC
        """
        params = (ticker, start_time)
    elif interval == "4h":
        sql = f"""
            SELECT 
                date_trunc('hour', timestamp) - (extract(hour from timestamp)::int %% 4) * interval '1 hour' AS time,
                (array_agg(open ORDER BY timestamp ASC))[1] AS open,
                MAX(high) AS high,
                MIN(low) AS low,
                (array_agg(close ORDER BY timestamp DESC))[1] AS close,
                SUM(volume) AS volume
            FROM {settings.mimir_schema}.mimir_hourly_ohlcv
            WHERE ticker = %s AND timestamp >= %s
            GROUP BY 1
            ORDER BY time ASC
        """
        params = (ticker, start_time)
    else: # 1d
        sql = f"""
            SELECT 
                date_trunc('day', timestamp) AS time,
                (array_agg(open ORDER BY timestamp ASC))[1] AS open,
                MAX(high) AS high,
                MIN(low) AS low,
                (array_agg(close ORDER BY timestamp DESC))[1] AS close,
                SUM(volume) AS volume
            FROM {settings.mimir_schema}.mimir_hourly_ohlcv
            WHERE ticker = %s AND timestamp >= %s
            GROUP BY 1
            ORDER BY time ASC
        """
        params = (ticker, start_time)
        
    try:
        cur.execute(sql, params)
        candles = cur.fetchall()
    except Exception as e:
        cur.close()
        conn.close()
        raise HTTPException(status_code=500, detail=f"Database aggregation failed: {e}")
        
    cur.close()
    conn.close()
    
    return {
        "ticker": ticker,
        "interval": interval,
        "candles": [
            {
                "time": c["time"],
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "volume": int(c["volume"]) if c["volume"] else 0
            }
            for c in candles
        ]
    }

# In-memory cache for ticker info (logo URL + short name)
_ticker_info_cache = {}

@router.get("/prices/logos")
async def get_ticker_logos(tickers: str = Query(...)):
    """
    Get logo URLs and short names for the given comma-separated tickers.
    Results are cached in memory to avoid redundant yfinance calls.
    """
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    print(f"[DEBUG LOGOS] Ticker list: {ticker_list}")
    result = {}

    for ticker_symbol in ticker_list:
        print(f"[DEBUG LOGOS] Processing {ticker_symbol}, in cache: {ticker_symbol in _ticker_info_cache}")
        if ticker_symbol in _ticker_info_cache:
            result[ticker_symbol] = _ticker_info_cache[ticker_symbol]
            continue

        try:
            session = Session(impersonate="chrome", verify=False)
            session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9"
            })
            ticker = yf.Ticker(ticker_symbol, session=session)
            info = ticker.info
            if not isinstance(info, dict):
                info = {}
            logo_url = f"https://finance-logo.perplexity.ai/ticker/{ticker_symbol}?format=png&fallback=404&size=50&theme=dark"
            print(f"[DEBUG LOGOS] set perplexity logo url for {ticker_symbol}: {logo_url}")

            entry = {
                "logo_url": logo_url,
                "long_name": info.get("longName", "")
            }
            _ticker_info_cache[ticker_symbol] = entry
            result[ticker_symbol] = entry
            time.sleep(0.5)
        except Exception as e:
            print(f"[TICKER_INFO] Error fetching info for {ticker_symbol}: {e}")
            entry = {"logo_url": "", "long_name": ""}
            _ticker_info_cache[ticker_symbol] = entry
            result[ticker_symbol] = entry

    return {"tickers": result}

