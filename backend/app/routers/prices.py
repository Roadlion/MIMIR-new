# backend/app/routers/prices.py
from fastapi import APIRouter, Query, HTTPException
from typing import List, Optional
from datetime import datetime, timedelta, timezone
import yfinance as yf
from yfinance import cache as yf_cache
import pandas as pd

# Disable yfinance sqlite disk cookie cache to avoid persistent 401 crumb errors
try:
    yf_cache.get_cookie_cache().dummy = True
    print("[YFINANCE] cookie disk cache disabled (dummy=True)")
except Exception as e:
    print(f"[YFINANCE] failed to disable cookie cache: {e}")
import psycopg2
import time
import warnings
warnings.filterwarnings("ignore", message="Unverified HTTPS request")
from concurrent.futures import ThreadPoolExecutor
from curl_cffi.requests import Session
from psycopg2.extras import execute_values, RealDictCursor
from ..database import get_db_connection_dict, get_db_connection
from ..config import get_settings

router = APIRouter()
settings = get_settings()

DEFAULT_TICKERS = [
    # Stock Indices
    "SPY", "QQQ", "^DJI", "^VIX", "^N225", "000300.SS", "^KS11", "^SET50.BK", 
    "^FTSE", "^GDAXI", "^FCHI", "FTSEMIB.MI", "^IBEX", "^STOXX50E", "^NSEI", 
    "^GSPTSE", "^AXJO", "^BVSP",
    # Currencies (FX)
    "DX-Y.NYB", "EURUSD=X", "USDJPY=X", "GBPUSD=X", "USDCHF=X", "USDCNY=X", 
    "USDTHB=X", "USDKRW=X", "USDINR=X", "USDCAD=X", "AUDUSD=X", "USDBRL=X",
    # Commodities
    "GC=F", "CL=F",
    # Cryptos
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "BNB-USD", "ADA-USD",
    # Sectors (US GICS)
    "XLF", "XLB", "XLC", "XLRE", "XLU", "XLE", "XLI", "XLP", "XLK", "XLY", "XLV",
    # Bond Yields
    "^TNX", "^FVX", "^TYX",
    # Benchmark Equities
    "AAPL", "MSFT", "NVDA", "TSLA"
]

def fetch_and_cache_ticker(ticker_symbol: str, conn=None):
    """
    Fetches 7 days of hourly price history (OHLCV) for the ticker using yfinance
    and inserts/caches it into the SQL database.
    """
    ticker_symbol = ticker_symbol.strip().lstrip('$').upper()
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
        # Pause briefly to avoid hammering
        time.sleep(0.5)
        
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
            
        # Open DB connection ONLY when inserting
        close_conn = False
        if conn is None:
            conn = get_db_connection()
            close_conn = True
        try:
            cur = conn.cursor()
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
        finally:
            if close_conn and conn:
                conn.close()
    except Exception as e:
        print(f"[YFINANCE] Error fetching/caching {ticker_symbol}: {e}")
        return False

def fetch_and_cache_daily_ticker(ticker_symbol: str, conn=None):
    """
    Fetches 1 year of daily price history for the ticker using yfinance
    and caches it into the hourly table (as daily rows at market-close timestamps).
    """
    ticker_symbol = ticker_symbol.strip().lstrip('$').upper()
    print(f"[YFINANCE] Starting daily fetch for {ticker_symbol} (1y)...")
    try:
        session = Session(impersonate="chrome")
        session.verify = False
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9"
        })
        ticker = yf.Ticker(ticker_symbol, session=session)
        df = ticker.history(period="1y", interval="1d")
        time.sleep(0.5)

        if df.empty:
            print(f"[YFINANCE] Empty daily DataFrame for {ticker_symbol}")
            return False

        records = []
        for index, row in df.iterrows():
            ts = index.to_pydatetime()
            records.append((
                ticker_symbol, ts,
                float(row["Open"]), float(row["High"]),
                float(row["Low"]), float(row["Close"]),
                int(row["Volume"]) if "Volume" in row else 0
            ))

        if not records:
            return False

        # Open DB connection ONLY when inserting
        close_conn = False
        if conn is None:
            conn = get_db_connection()
            close_conn = True
        try:
            cur = conn.cursor()
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
            print(f"[YFINANCE] Cached {len(records)} daily records for {ticker_symbol}")
            return True
        finally:
            if close_conn and conn:
                conn.close()
    except Exception as e:
        print(f"[YFINANCE] Error fetching daily cache for {ticker_symbol}: {e}")
        return False

def fetch_and_cache_tickers_concurrently(tickers: List[str]):
    """
    Fetches and caches prices for multiple tickers concurrently using a thread pool.
    Each thread uses its own database connection.
    """
    if not tickers:
        return
    print(f"[YFINANCE] Concurrently fetching {len(tickers)} tickers...")
    max_workers = min(len(tickers), 5)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        executor.map(lambda t: fetch_and_cache_ticker(t, None), tickers)

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
        if now_ts - last_scraped > timedelta(minutes=2):
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
    Uses bulk queries to prevent database roundtrip overhead and keeps logs clean.
    """
    ticker_list = DEFAULT_TICKERS
    if tickers:
        ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
        
    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    # 1. Bulk check when tickers were last scraped
    cur.execute(f"""
        SELECT ticker, MAX(scraped_at) as last_scraped 
        FROM {settings.mimir_schema}.mimir_hourly_ohlcv 
        WHERE ticker = ANY(%s)
        GROUP BY ticker
    """, (ticker_list,))
    rows = cur.fetchall()
    last_scraped_map = {r["ticker"]: r["last_scraped"] for r in rows}
    
    # Identify which tickers are missing or stale (>2m)
    now_ts = datetime.now(timezone.utc)
    stale_tickers = []
    for ticker in ticker_list:
        last_scraped = last_scraped_map.get(ticker)
        if not last_scraped or (now_ts - last_scraped > timedelta(minutes=2)):
            stale_tickers.append(ticker)
            
    # Fetch from yfinance concurrently only for stale tickers
    if stale_tickers:
        print(f"[DATABASE] Cache miss/stale for tickers: {stale_tickers}. Fetching from yfinance concurrently...")
        fetch_and_cache_tickers_concurrently(stale_tickers)
        
    # 2. Bulk fetch latest price and close price nearest to 24h ago

    cur.execute(f"""
        WITH latest_prices AS (
            SELECT DISTINCT ON (ticker)
                ticker,
                close AS latest_price,
                timestamp AS latest_ts
            FROM {settings.mimir_schema}.mimir_hourly_ohlcv
            WHERE ticker = ANY(%s)
            ORDER BY ticker, timestamp DESC
        ),
        prev_prices AS (
            SELECT DISTINCT ON (l.ticker)
                l.ticker,
                h.close AS prev_price
            FROM latest_prices l
            JOIN {settings.mimir_schema}.mimir_hourly_ohlcv h ON l.ticker = h.ticker
            WHERE h.timestamp <= l.latest_ts - INTERVAL '24 hours'
            ORDER BY l.ticker, h.timestamp DESC
        )
        SELECT 
            l.ticker,
            l.latest_price,
            COALESCE(p.prev_price, (
                SELECT close FROM {settings.mimir_schema}.mimir_hourly_ohlcv h2 
                WHERE h2.ticker = l.ticker 
                ORDER BY timestamp ASC LIMIT 1
            )) as prev_price
        FROM latest_prices l
        LEFT JOIN prev_prices p ON l.ticker = p.ticker
    """, (ticker_list,))
    
    price_rows = cur.fetchall()
    cur.close()
    conn.close()
    
    # Map rows to the expected API schema preserving requested ticker list order
    price_map = {r["ticker"]: r for r in price_rows}
    results = []
    for ticker in ticker_list:
        r = price_map.get(ticker)
        if r:
            latest_price = float(r["latest_price"])
            prev_price = float(r["prev_price"]) if r["prev_price"] is not None else latest_price
            change_percent = 0.0
            if prev_price > 0:
                change_percent = ((latest_price - prev_price) / prev_price) * 100
            results.append({
                "ticker": ticker,
                "current_price": latest_price,
                "price_24h_ago": prev_price,
                "change_percent": round(change_percent, 2)
            })
            
    return {"tickers": results}

@router.get("/prices/candles")
async def get_candles(
    ticker: str,
    interval: str = Query("1h", pattern="^(1m|5m|15m|30m|1h|4h|1d)$"),
    days: int = Query(7, ge=1, le=365)
):
    """
    Get aggregated candlestick (OHLCV) data for a given ticker and time interval.
    Supported intervals: '1m', '5m', '15m', '30m', '1h', '4h', '1d'.
    """
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # Decide source table based on interval
    is_minute = interval in ["1m", "5m", "15m", "30m"]
    table_name = "mimir_minute_ohlcv" if is_minute else "mimir_hourly_ohlcv"
    
    # Ensure some data exists in cache for the ticker
    cur.execute(f"""
        SELECT EXISTS(
            SELECT 1 FROM {settings.mimir_schema}.{table_name} 
            WHERE ticker = %s
        )
    """, (ticker,))
    has_data = cur.fetchone()["exists"]
    
    if not has_data:
        # If minute table is empty, we can fetch dynamic ticker or cache ticker
        if is_minute:
            from backend.app.pipeline.background_worker import fetch_and_cache_minute_ticker
            fetch_and_cache_minute_ticker(ticker, conn)
        else:
            fetch_and_cache_ticker(ticker, conn)

    # For 1d interval with >7 days, the 7d hourly cache won't cover it — fetch daily data if stale
    if interval == "1d" and days > 7:
        cur.execute(f"""
            SELECT MAX(scraped_at) as last_scraped
            FROM {settings.mimir_schema}.mimir_hourly_ohlcv
            WHERE ticker = %s AND timestamp >= NOW() - INTERVAL '{days} days'
        """, (ticker,))
        row = cur.fetchone()
        last_scraped = row["last_scraped"] if row else None
        is_stale = not last_scraped or (datetime.now(timezone.utc) - last_scraped > timedelta(hours=6))
        if is_stale:
            fetch_and_cache_daily_ticker(ticker, conn)

    start_time = datetime.now(timezone.utc) - timedelta(days=days)
    
    # Select aggregation query based on requested interval
    if interval == "1m":
        sql = f"""
            SELECT 
                timestamp AS time,
                open, high, low, close, volume
            FROM {settings.mimir_schema}.mimir_minute_ohlcv
            WHERE ticker = %s AND timestamp >= %s
            ORDER BY timestamp ASC
        """
        params = (ticker, start_time)
    elif interval in ["5m", "15m", "30m"]:
        minutes_group = int(interval[:-1])
        sql = f"""
            SELECT 
                date_trunc('hour', timestamp) + (extract(minute from timestamp)::int / {minutes_group}) * interval '{minutes_group} minutes' AS time,
                (array_agg(open ORDER BY timestamp ASC))[1] AS open,
                MAX(high) AS high,
                MIN(low) AS low,
                (array_agg(close ORDER BY timestamp DESC))[1] AS close,
                SUM(volume) AS volume
            FROM {settings.mimir_schema}.mimir_minute_ohlcv
            WHERE ticker = %s AND timestamp >= %s
            GROUP BY 1
            ORDER BY time ASC
        """
        params = (ticker, start_time)
    elif interval == "1h":
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
        
        # Fetch historical sentiments
        sent_rows = []
        try:
            cur.execute(f"""
                SELECT 
                    COALESCE(a.published_ts, a.scraped_at) AS time,
                    si.sentiment_score
                FROM {settings.mimir_schema}.mimir_raw_articles a
                JOIN {settings.mimir_schema}.mimir_sentiment_impacts si ON a.id = si.article_id
                WHERE si.ticker = %s AND COALESCE(a.published_ts, a.scraped_at) >= %s
                
                UNION ALL
                
                SELECT 
                    bucket_ts AS time,
                    sentiment_score
                FROM {settings.mimir_schema}.mimir_social_chatter
                WHERE ticker = %s AND bucket_ts >= %s
                
                ORDER BY time ASC
            """, (ticker, start_time - timedelta(days=1), ticker, start_time - timedelta(days=1)))
            sent_rows = cur.fetchall()
        except Exception as sent_err:
            print(f"[CANDLES] Sentiment fetch error: {sent_err}")
            
    except Exception as e:
        cur.close()
        conn.close()
        raise HTTPException(status_code=500, detail=f"Database aggregation failed: {e}")
        
    cur.close()
    conn.close()
    
    # helper for tz-naive datetimes
    def make_naive(dt):
        if dt is None:
            return None
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

    # Aggregate rolling sentiment for each candle
    candles_list = []
    current_sentiment = 0.0
    
    # Convert sentiment times to naive datetimes once
    parsed_sent = []
    for s in sent_rows:
        s_time = make_naive(s["time"])
        if s_time and s["sentiment_score"] is not None:
            parsed_sent.append({
                "time": s_time,
                "score": float(s["sentiment_score"])
            })
            
    for c in candles:
        c_time = make_naive(c["time"])
        
        # 24 hour rolling window
        window_start = c_time - timedelta(hours=24)
        scores_in_window = [s["score"] for s in parsed_sent if window_start <= s["time"] <= c_time]
        
        if scores_in_window:
            current_sentiment = sum(scores_in_window) / len(scores_in_window)
        else:
            # Fallback to the latest score prior to c_time
            prior_scores = [s["score"] for s in parsed_sent if s["time"] <= c_time]
            if prior_scores:
                current_sentiment = prior_scores[-1]
            else:
                current_sentiment = 0.0
                
        candles_list.append({
            "time": c["time"],
            "open": float(c["open"]) if c["open"] is not None else 0.0,
            "high": float(c["high"]) if c["high"] is not None else 0.0,
            "low": float(c["low"]) if c["low"] is not None else 0.0,
            "close": float(c["close"]) if c["close"] is not None else 0.0,
            "volume": int(c["volume"]) if c["volume"] else 0,
            "sentiment": round(current_sentiment, 4)
        })

    return {
        "ticker": ticker,
        "interval": interval,
        "days": days,
        "candles": candles_list
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


# ===================== HEATMAP DATA =====================

# Index constituent definitions: {display_name: {ticker: {name, sector, weight}}}
# Weights approximate market cap weights (used for treemap sizing)
HEATMAP_INDICES = {
    "sp500": {
        "label": "S&P 500",
        "constituents": [
            # Technology
            {"ticker": "NVDA",  "name": "NVIDIA",           "sector": "Technology"},
            {"ticker": "AAPL",  "name": "Apple",            "sector": "Technology"},
            {"ticker": "MSFT",  "name": "Microsoft",        "sector": "Technology"},
            {"ticker": "AVGO",  "name": "Broadcom",         "sector": "Technology"},
            {"ticker": "ORCL",  "name": "Oracle",           "sector": "Technology"},
            {"ticker": "CRM",   "name": "Salesforce",       "sector": "Technology"},
            {"ticker": "AMD",   "name": "AMD",              "sector": "Technology"},
            {"ticker": "QCOM",  "name": "Qualcomm",         "sector": "Technology"},
            {"ticker": "INTC",  "name": "Intel",            "sector": "Technology"},
            {"ticker": "TXN",   "name": "Texas Instruments","sector": "Technology"},
            {"ticker": "MU",    "name": "Micron",           "sector": "Technology"},
            {"ticker": "AMAT",  "name": "Applied Materials","sector": "Technology"},
            {"ticker": "ADI",   "name": "Analog Devices",   "sector": "Technology"},
            {"ticker": "KLAC",  "name": "KLA Corp",         "sector": "Technology"},
            # Communication Services
            {"ticker": "META",  "name": "Meta",             "sector": "Comm. Services"},
            {"ticker": "GOOGL", "name": "Alphabet",         "sector": "Comm. Services"},
            {"ticker": "NFLX",  "name": "Netflix",          "sector": "Comm. Services"},
            {"ticker": "DIS",   "name": "Disney",           "sector": "Comm. Services"},
            {"ticker": "T",     "name": "AT&T",             "sector": "Comm. Services"},
            {"ticker": "VZ",    "name": "Verizon",          "sector": "Comm. Services"},
            # Consumer Cyclical
            {"ticker": "AMZN",  "name": "Amazon",           "sector": "Consumer Cycl."},
            {"ticker": "TSLA",  "name": "Tesla",            "sector": "Consumer Cycl."},
            {"ticker": "HD",    "name": "Home Depot",       "sector": "Consumer Cycl."},
            {"ticker": "MCD",   "name": "McDonald's",       "sector": "Consumer Cycl."},
            {"ticker": "NKE",   "name": "Nike",             "sector": "Consumer Cycl."},
            {"ticker": "TJX",   "name": "TJX Companies",   "sector": "Consumer Cycl."},
            {"ticker": "LOW",   "name": "Lowe's",           "sector": "Consumer Cycl."},
            # Financial Services
            {"ticker": "BRK-B", "name": "Berkshire",        "sector": "Financials"},
            {"ticker": "JPM",   "name": "JPMorgan",         "sector": "Financials"},
            {"ticker": "V",     "name": "Visa",             "sector": "Financials"},
            {"ticker": "MA",    "name": "Mastercard",       "sector": "Financials"},
            {"ticker": "BAC",   "name": "Bank of America",  "sector": "Financials"},
            {"ticker": "WFC",   "name": "Wells Fargo",      "sector": "Financials"},
            {"ticker": "GS",    "name": "Goldman Sachs",    "sector": "Financials"},
            {"ticker": "MS",    "name": "Morgan Stanley",   "sector": "Financials"},
            # Healthcare
            {"ticker": "LLY",   "name": "Eli Lilly",        "sector": "Healthcare"},
            {"ticker": "UNH",   "name": "UnitedHealth",     "sector": "Healthcare"},
            {"ticker": "JNJ",   "name": "J&J",              "sector": "Healthcare"},
            {"ticker": "ABBV",  "name": "AbbVie",           "sector": "Healthcare"},
            {"ticker": "MRK",   "name": "Merck",            "sector": "Healthcare"},
            {"ticker": "TMO",   "name": "Thermo Fisher",    "sector": "Healthcare"},
            {"ticker": "ISRG",  "name": "Intuitive Surgical","sector": "Healthcare"},
            {"ticker": "GILD",  "name": "Gilead",           "sector": "Healthcare"},
            # Consumer Defensive
            {"ticker": "WMT",   "name": "Walmart",          "sector": "Consumer Def."},
            {"ticker": "COST",  "name": "Costco",           "sector": "Consumer Def."},
            {"ticker": "KO",    "name": "Coca-Cola",        "sector": "Consumer Def."},
            {"ticker": "PEP",   "name": "PepsiCo",          "sector": "Consumer Def."},
            {"ticker": "PG",    "name": "Procter & Gamble", "sector": "Consumer Def."},
            {"ticker": "PM",    "name": "Philip Morris",    "sector": "Consumer Def."},
            # Energy
            {"ticker": "XOM",   "name": "ExxonMobil",       "sector": "Energy"},
            {"ticker": "CVX",   "name": "Chevron",          "sector": "Energy"},
            {"ticker": "COP",   "name": "ConocoPhillips",   "sector": "Energy"},
            {"ticker": "SLB",   "name": "SLB",              "sector": "Energy"},
            # Industrials
            {"ticker": "CAT",   "name": "Caterpillar",      "sector": "Industrials"},
            {"ticker": "GE",    "name": "GE Aerospace",     "sector": "Industrials"},
            {"ticker": "UNP",   "name": "Union Pacific",    "sector": "Industrials"},
            {"ticker": "BA",    "name": "Boeing",           "sector": "Industrials"},
            {"ticker": "HON",   "name": "Honeywell",        "sector": "Industrials"},
            {"ticker": "LIN",   "name": "Linde",            "sector": "Industrials"},
            # Utilities
            {"ticker": "NEE",   "name": "NextEra Energy",   "sector": "Utilities"},
            {"ticker": "SO",    "name": "Southern Company",  "sector": "Utilities"},
            {"ticker": "DUK",   "name": "Duke Energy",      "sector": "Utilities"},
        ]
    },
    "set50": {
        "label": "SET 50",
        "constituents": [
            {"ticker": "PTT.BK",    "name": "PTT",              "sector": "Energy"},
            {"ticker": "PTTEP.BK",  "name": "PTTEP",            "sector": "Energy"},
            {"ticker": "GULF.BK",   "name": "Gulf Energy",      "sector": "Energy"},
            {"ticker": "GPSC.BK",   "name": "GPSC",             "sector": "Energy"},
            {"ticker": "RATCH.BK",  "name": "Ratch Group",      "sector": "Energy"},
            {"ticker": "CPALL.BK",  "name": "CP All",           "sector": "Consumer Def."},
            {"ticker": "HMPRO.BK",  "name": "HomePro",          "sector": "Consumer Cycl."},
            {"ticker": "BJC.BK",    "name": "BJC",              "sector": "Consumer Cycl."},
            {"ticker": "MINT.BK",   "name": "MINT",             "sector": "Consumer Cycl."},
            {"ticker": "AOT.BK",    "name": "Airports of Thailand","sector": "Industrials"},
            {"ticker": "BTS.BK",    "name": "BTS Group",        "sector": "Industrials"},
            {"ticker": "WHA.BK",    "name": "WHA Corp",         "sector": "Industrials"},
            {"ticker": "SCB.BK",    "name": "SCB",              "sector": "Financials"},
            {"ticker": "KBANK.BK",  "name": "Kasikornbank",     "sector": "Financials"},
            {"ticker": "BBL.BK",    "name": "Bangkok Bank",     "sector": "Financials"},
            {"ticker": "KTB.BK",    "name": "Krungthai Bank",   "sector": "Financials"},
            {"ticker": "BAY.BK",    "name": "Bank of Ayudhya",  "sector": "Financials"},
            {"ticker": "MBK.BK",    "name": "MBK",              "sector": "Financials"},
            {"ticker": "ADVANC.BK", "name": "AIS",              "sector": "Comm. Services"},
            {"ticker": "TRUE.BK",   "name": "True Corporation", "sector": "Comm. Services"},
            {"ticker": "INTUCH.BK", "name": "Intouch",          "sector": "Comm. Services"},
            {"ticker": "SCC.BK",    "name": "SCG",              "sector": "Materials"},
            {"ticker": "PTTGC.BK",  "name": "PTT GC",          "sector": "Materials"},
            {"ticker": "IVL.BK",    "name": "Indorama Ventures","sector": "Materials"},
            {"ticker": "BDMS.BK",   "name": "BDMS",             "sector": "Healthcare"},
            {"ticker": "BH.BK",     "name": "Bumrungrad",       "sector": "Healthcare"},
            {"ticker": "DELTA.BK",  "name": "Delta Electronics","sector": "Technology"},
            {"ticker": "HANA.BK",   "name": "Hana Microelectronics","sector": "Technology"},
            {"ticker": "CRC.BK",    "name": "Central Retail",   "sector": "Consumer Cycl."},
            {"ticker": "CPN.BK",    "name": "CPN",              "sector": "Real Estate"},
        ]
    },
    "kospi200": {
        "label": "KOSPI 200",
        "constituents": [
            {"ticker": "005930.KS", "name": "Samsung Electronics","sector": "Technology"},
            {"ticker": "000660.KS", "name": "SK Hynix",         "sector": "Technology"},
            {"ticker": "035420.KS", "name": "NAVER",            "sector": "Technology"},
            {"ticker": "035720.KS", "name": "Kakao",            "sector": "Technology"},
            {"ticker": "066570.KS", "name": "LG Electronics",   "sector": "Technology"},
            {"ticker": "207940.KS", "name": "Samsung Biologics","sector": "Healthcare"},
            {"ticker": "068270.KS", "name": "Celltrion",        "sector": "Healthcare"},
            {"ticker": "128940.KS", "name": "Hanmi Pharm",      "sector": "Healthcare"},
            {"ticker": "373220.KS", "name": "LG Energy Solution","sector": "Industrials"},
            {"ticker": "051910.KS", "name": "LG Chem",          "sector": "Materials"},
            {"ticker": "005490.KS", "name": "POSCO Holdings",   "sector": "Materials"},
            {"ticker": "000270.KS", "name": "Kia",              "sector": "Consumer Cycl."},
            {"ticker": "005380.KS", "name": "Hyundai Motor",    "sector": "Consumer Cycl."},
            {"ticker": "009540.KS", "name": "HD Hyundai",       "sector": "Industrials"},
            {"ticker": "105560.KS", "name": "KB Financial",     "sector": "Financials"},
            {"ticker": "055550.KS", "name": "Shinhan Financial","sector": "Financials"},
            {"ticker": "086790.KS", "name": "Hana Financial",   "sector": "Financials"},
            {"ticker": "018260.KS", "name": "Samsung SDS",      "sector": "Technology"},
            {"ticker": "028260.KS", "name": "Samsung C&T",      "sector": "Industrials"},
            {"ticker": "034020.KS", "name": "Doosan Enerbility","sector": "Industrials"},
            {"ticker": "032830.KS", "name": "Samsung Life",     "sector": "Financials"},
            {"ticker": "017670.KS", "name": "SK Telecom",       "sector": "Comm. Services"},
            {"ticker": "030200.KS", "name": "KT Corp",          "sector": "Comm. Services"},
            {"ticker": "003550.KS", "name": "LG Corp",          "sector": "Industrials"},
            {"ticker": "011200.KS", "name": "HMM",              "sector": "Industrials"},
        ]
    },
    "nifty50": {
        "label": "NIFTY 50",
        "constituents": [
            {"ticker": "RELIANCE.NS",  "name": "Reliance Industries","sector": "Energy"},
            {"ticker": "TCS.NS",       "name": "TCS",               "sector": "Technology"},
            {"ticker": "HDFCBANK.NS",  "name": "HDFC Bank",         "sector": "Financials"},
            {"ticker": "INFY.NS",      "name": "Infosys",           "sector": "Technology"},
            {"ticker": "ICICIBANK.NS", "name": "ICICI Bank",        "sector": "Financials"},
            {"ticker": "BHARTIARTL.NS","name": "Bharti Airtel",     "sector": "Comm. Services"},
            {"ticker": "KOTAKBANK.NS", "name": "Kotak Mahindra",    "sector": "Financials"},
            {"ticker": "HINDUNILVR.NS","name": "Hindustan Unilever","sector": "Consumer Def."},
            {"ticker": "ITC.NS",       "name": "ITC",               "sector": "Consumer Def."},
            {"ticker": "SBIN.NS",      "name": "State Bank of India","sector": "Financials"},
            {"ticker": "LT.NS",        "name": "Larsen & Toubro",   "sector": "Industrials"},
            {"ticker": "BAJFINANCE.NS","name": "Bajaj Finance",     "sector": "Financials"},
            {"ticker": "AXISBANK.NS",  "name": "Axis Bank",         "sector": "Financials"},
            {"ticker": "MARUTI.NS",    "name": "Maruti Suzuki",     "sector": "Consumer Cycl."},
            {"ticker": "SUNPHARMA.NS", "name": "Sun Pharma",        "sector": "Healthcare"},
            {"ticker": "WIPRO.NS",     "name": "Wipro",             "sector": "Technology"},
            {"ticker": "TITAN.NS",     "name": "Titan Company",     "sector": "Consumer Cycl."},
            {"ticker": "HCLTECH.NS",   "name": "HCL Technologies",  "sector": "Technology"},
            {"ticker": "ULTRACEMCO.NS","name": "UltraTech Cement",  "sector": "Materials"},
            {"ticker": "ADANIENT.NS",  "name": "Adani Enterprises", "sector": "Industrials"},
            {"ticker": "NTPC.NS",      "name": "NTPC",              "sector": "Utilities"},
            {"ticker": "POWERGRID.NS", "name": "Power Grid",        "sector": "Utilities"},
            {"ticker": "TATASTEEL.NS", "name": "Tata Steel",        "sector": "Materials"},
            {"ticker": "ONGC.NS",      "name": "ONGC",              "sector": "Energy"},
            {"ticker": "COALINDIA.NS", "name": "Coal India",        "sector": "Energy"},
        ]
    },
    "nikkei225": {
        "label": "Nikkei 225",
        "constituents": [
            {"ticker": "7203.T",  "name": "Toyota Motor",      "sector": "Consumer Cycl."},
            {"ticker": "6758.T",  "name": "Sony Group",        "sector": "Technology"},
            {"ticker": "8306.T",  "name": "Mitsubishi UFJ",    "sector": "Financials"},
            {"ticker": "9432.T",  "name": "NTT",               "sector": "Comm. Services"},
            {"ticker": "6861.T",  "name": "Keyence",           "sector": "Technology"},
            {"ticker": "8035.T",  "name": "Tokyo Electron",    "sector": "Technology"},
            {"ticker": "7267.T",  "name": "Honda Motor",       "sector": "Consumer Cycl."},
            {"ticker": "6501.T",  "name": "Hitachi",           "sector": "Industrials"},
            {"ticker": "9984.T",  "name": "SoftBank Group",    "sector": "Technology"},
            {"ticker": "6702.T",  "name": "Fujitsu",           "sector": "Technology"},
            {"ticker": "7974.T",  "name": "Nintendo",          "sector": "Technology"},
            {"ticker": "6954.T",  "name": "Fanuc",             "sector": "Industrials"},
            {"ticker": "8316.T",  "name": "Sumitomo Mitsui",   "sector": "Financials"},
            {"ticker": "8411.T",  "name": "Mizuho Financial",  "sector": "Financials"},
            {"ticker": "4063.T",  "name": "Shin-Etsu Chemical","sector": "Materials"},
            {"ticker": "4568.T",  "name": "Daiichi Sankyo",    "sector": "Healthcare"},
            {"ticker": "4502.T",  "name": "Takeda Pharma",     "sector": "Healthcare"},
            {"ticker": "7733.T",  "name": "Olympus",           "sector": "Healthcare"},
            {"ticker": "9433.T",  "name": "KDDI",              "sector": "Comm. Services"},
            {"ticker": "6098.T",  "name": "Recruit Holdings",  "sector": "Industrials"},
            {"ticker": "2802.T",  "name": "Ajinomoto",         "sector": "Consumer Def."},
            {"ticker": "4452.T",  "name": "Kao",               "sector": "Consumer Def."},
            {"ticker": "9020.T",  "name": "East Japan Railway","sector": "Industrials"},
            {"ticker": "8591.T",  "name": "ORIX",              "sector": "Financials"},
        ]
    },
    "ftse100": {
        "label": "FTSE 100",
        "constituents": [
            {"ticker": "SHEL.L",  "name": "Shell",             "sector": "Energy"},
            {"ticker": "AZN.L",   "name": "AstraZeneca",       "sector": "Healthcare"},
            {"ticker": "HSBA.L",  "name": "HSBC Holdings",     "sector": "Financials"},
            {"ticker": "ULVR.L",  "name": "Unilever",          "sector": "Consumer Def."},
            {"ticker": "BP.L",    "name": "BP",                "sector": "Energy"},
            {"ticker": "LSEG.L",  "name": "London Stock Exch.","sector": "Financials"},
            {"ticker": "GSK.L",   "name": "GSK",               "sector": "Healthcare"},
            {"ticker": "RIO.L",   "name": "Rio Tinto",         "sector": "Materials"},
            {"ticker": "DGE.L",   "name": "Diageo",            "sector": "Consumer Def."},
            {"ticker": "BATS.L",  "name": "British American Tobacco","sector": "Consumer Def."},
            {"ticker": "REL.L",   "name": "Relx",              "sector": "Industrials"},
            {"ticker": "NG.L",    "name": "National Grid",     "sector": "Utilities"},
            {"ticker": "AAL.L",   "name": "Anglo American",    "sector": "Materials"},
            {"ticker": "LLOY.L",  "name": "Lloyds Banking",    "sector": "Financials"},
            {"ticker": "BARC.L",  "name": "Barclays",          "sector": "Financials"},
            {"ticker": "BA.L",    "name": "BAE Systems",       "sector": "Industrials"},
            {"ticker": "RKT.L",   "name": "Reckitt",           "sector": "Consumer Def."},
            {"ticker": "VOD.L",   "name": "Vodafone",          "sector": "Comm. Services"},
            {"ticker": "TSCO.L",  "name": "Tesco",             "sector": "Consumer Def."},
            {"ticker": "IMB.L",   "name": "Imperial Brands",   "sector": "Consumer Def."},
            {"ticker": "EXPN.L",  "name": "Experian",          "sector": "Industrials"},
            {"ticker": "CRH.L",   "name": "CRH",               "sector": "Materials"},
            {"ticker": "PRU.L",   "name": "Prudential",        "sector": "Financials"},
            {"ticker": "NWG.L",   "name": "NatWest Group",     "sector": "Financials"},
        ]
    },
    "dax": {
        "label": "DAX",
        "constituents": [
            {"ticker": "SAP.DE",  "name": "SAP",               "sector": "Technology"},
            {"ticker": "SIE.DE",  "name": "Siemens",           "sector": "Industrials"},
            {"ticker": "ALV.DE",  "name": "Allianz",           "sector": "Financials"},
            {"ticker": "DTE.DE",  "name": "Deutsche Telekom",  "sector": "Comm. Services"},
            {"ticker": "MBG.DE",  "name": "Mercedes-Benz",     "sector": "Consumer Cycl."},
            {"ticker": "BAYN.DE", "name": "Bayer",             "sector": "Healthcare"},
            {"ticker": "BMW.DE",  "name": "BMW",               "sector": "Consumer Cycl."},
            {"ticker": "MRK.DE",  "name": "Merck KGaA",        "sector": "Healthcare"},
            {"ticker": "DBK.DE",  "name": "Deutsche Bank",     "sector": "Financials"},
            {"ticker": "EOAN.DE", "name": "E.ON",              "sector": "Utilities"},
            {"ticker": "ADS.DE",  "name": "Adidas",            "sector": "Consumer Cycl."},
            {"ticker": "BAS.DE",  "name": "BASF",              "sector": "Materials"},
            {"ticker": "VOW3.DE", "name": "Volkswagen",        "sector": "Consumer Cycl."},
            {"ticker": "MUV2.DE", "name": "Munich Re",         "sector": "Financials"},
            {"ticker": "HEN3.DE", "name": "Henkel",            "sector": "Consumer Def."},
            {"ticker": "SHL.DE",  "name": "Siemens Healthineers","sector": "Healthcare"},
            {"ticker": "IFX.DE",  "name": "Infineon",          "sector": "Technology"},
            {"ticker": "RWE.DE",  "name": "RWE",               "sector": "Utilities"},
            {"ticker": "ZAL.DE",  "name": "Zalando",           "sector": "Consumer Cycl."},
            {"ticker": "P911.DE", "name": "Porsche AG",        "sector": "Consumer Cycl."},
        ]
    },
    "cac40": {
        "label": "CAC 40",
        "constituents": [
            {"ticker": "MC.PA",   "name": "LVMH",              "sector": "Consumer Cycl."},
            {"ticker": "TTE.PA",  "name": "TotalEnergies",     "sector": "Energy"},
            {"ticker": "SAN.PA",  "name": "Sanofi",            "sector": "Healthcare"},
            {"ticker": "AI.PA",   "name": "Air Liquide",       "sector": "Materials"},
            {"ticker": "OR.PA",   "name": "L'Oréal",           "sector": "Consumer Def."},
            {"ticker": "RMS.PA",  "name": "Hermès",            "sector": "Consumer Cycl."},
            {"ticker": "BNP.PA",  "name": "BNP Paribas",       "sector": "Financials"},
            {"ticker": "DG.PA",   "name": "Vinci",             "sector": "Industrials"},
            {"ticker": "ACA.PA",  "name": "Crédit Agricole",   "sector": "Financials"},
            {"ticker": "CS.PA",   "name": "AXA",               "sector": "Financials"},
            {"ticker": "AIR.PA",  "name": "Airbus",            "sector": "Industrials"},
            {"ticker": "CAP.PA",  "name": "Capgemini",         "sector": "Technology"},
            {"ticker": "SU.PA",   "name": "Schneider Electric","sector": "Industrials"},
            {"ticker": "DSY.PA",  "name": "Dassault Systèmes", "sector": "Technology"},
            {"ticker": "EL.PA",   "name": "EssilorLuxottica",  "sector": "Healthcare"},
            {"ticker": "KER.PA",  "name": "Kering",            "sector": "Consumer Cycl."},
            {"ticker": "GLE.PA",  "name": "Société Générale",  "sector": "Financials"},
            {"ticker": "STM.PA",  "name": "STMicroelectronics","sector": "Technology"},
            {"ticker": "EN.PA",   "name": "Bouygues",          "sector": "Industrials"},
            {"ticker": "FR.PA",   "name": "Valeo",             "sector": "Consumer Cycl."},
        ]
    },
    "stoxx600": {
        "label": "STOXX 600",
        "constituents": [
            # A broad pan-European selection
            {"ticker": "NESN.SW", "name": "Nestlé",            "sector": "Consumer Def."},
            {"ticker": "NOVO-B.CO","name": "Novo Nordisk",     "sector": "Healthcare"},
            {"ticker": "ROG.SW",  "name": "Roche",             "sector": "Healthcare"},
            {"ticker": "ASML.AS", "name": "ASML",              "sector": "Technology"},
            {"ticker": "AZN.L",   "name": "AstraZeneca",       "sector": "Healthcare"},
            {"ticker": "MC.PA",   "name": "LVMH",              "sector": "Consumer Cycl."},
            {"ticker": "SHEL.L",  "name": "Shell",             "sector": "Energy"},
            {"ticker": "SAP.DE",  "name": "SAP",               "sector": "Technology"},
            {"ticker": "HSBA.L",  "name": "HSBC",              "sector": "Financials"},
            {"ticker": "NOVN.SW", "name": "Novartis",          "sector": "Healthcare"},
            {"ticker": "TTE.PA",  "name": "TotalEnergies",     "sector": "Energy"},
            {"ticker": "SIE.DE",  "name": "Siemens",           "sector": "Industrials"},
            {"ticker": "ULVR.L",  "name": "Unilever",          "sector": "Consumer Def."},
            {"ticker": "SAN.PA",  "name": "Sanofi",            "sector": "Healthcare"},
            {"ticker": "BP.L",    "name": "BP",                "sector": "Energy"},
            {"ticker": "BNP.PA",  "name": "BNP Paribas",       "sector": "Financials"},
            {"ticker": "RIO.L",   "name": "Rio Tinto",         "sector": "Materials"},
            {"ticker": "GSK.L",   "name": "GSK",               "sector": "Healthcare"},
            {"ticker": "AIR.PA",  "name": "Airbus",            "sector": "Industrials"},
            {"ticker": "ALV.DE",  "name": "Allianz",           "sector": "Financials"},
            {"ticker": "MUV2.DE", "name": "Munich Re",         "sector": "Financials"},
            {"ticker": "OR.PA",   "name": "L'Oréal",           "sector": "Consumer Def."},
            {"ticker": "BAYN.DE", "name": "Bayer",             "sector": "Healthcare"},
            {"ticker": "DTE.DE",  "name": "Deutsche Telekom",  "sector": "Comm. Services"},
            {"ticker": "EOAN.DE", "name": "E.ON",              "sector": "Utilities"},
            {"ticker": "AI.PA",   "name": "Air Liquide",       "sector": "Materials"},
            {"ticker": "RMS.PA",  "name": "Hermès",            "sector": "Consumer Cycl."},
            {"ticker": "SU.PA",   "name": "Schneider Electric","sector": "Industrials"},
            {"ticker": "BAS.DE",  "name": "BASF",              "sector": "Materials"},
            {"ticker": "NG.L",    "name": "National Grid",     "sector": "Utilities"},
        ]
    },
    "spchina500": {
        "label": "S&P China 500",
        "constituents": [
            # Technology & Internet
            {"ticker": "0700.HK",   "name": "Tencent Holdings",      "sector": "Technology"},
            {"ticker": "9988.HK",   "name": "Alibaba Group",         "sector": "Technology"},
            {"ticker": "9618.HK",   "name": "JD.com",                "sector": "Consumer Cycl."},
            {"ticker": "9999.HK",   "name": "NetEase",               "sector": "Technology"},
            {"ticker": "BIDU",      "name": "Baidu",                 "sector": "Technology"},
            {"ticker": "9626.HK",   "name": "Bilibili",              "sector": "Technology"},
            {"ticker": "1024.HK",   "name": "Kuaishou Technology",   "sector": "Technology"},
            {"ticker": "3690.HK",   "name": "Meituan",               "sector": "Technology"},
            {"ticker": "9961.HK",   "name": "Trip.com Group",        "sector": "Consumer Cycl."},
            {"ticker": "6618.HK",   "name": "JD Health",             "sector": "Healthcare"},
            # Financials
            {"ticker": "0939.HK",   "name": "China Construction Bank","sector": "Financials"},
            {"ticker": "1398.HK",   "name": "ICBC",                  "sector": "Financials"},
            {"ticker": "3988.HK",   "name": "Bank of China",         "sector": "Financials"},
            {"ticker": "3328.HK",   "name": "Bank of Communications","sector": "Financials"},
            {"ticker": "2318.HK",   "name": "Ping An Insurance",     "sector": "Financials"},
            {"ticker": "2628.HK",   "name": "China Life Insurance",  "sector": "Financials"},
            {"ticker": "6030.HK",   "name": "CITIC Securities",      "sector": "Financials"},
            {"ticker": "0388.HK",   "name": "Hong Kong Exchanges",   "sector": "Financials"},
            # Consumer & Retail
            {"ticker": "9869.HK",   "name": "Miniso Group",          "sector": "Consumer Cycl."},
            {"ticker": "6862.HK",   "name": "Haidilao",              "sector": "Consumer Cycl."},
            {"ticker": "1929.HK",   "name": "Chow Tai Fook",         "sector": "Consumer Cycl."},
            {"ticker": "2331.HK",   "name": "Li Ning",               "sector": "Consumer Cycl."},
            {"ticker": "0960.HK",   "name": "Longfor Group",         "sector": "Real Estate"},
            # Energy & Utilities
            {"ticker": "0857.HK",   "name": "PetroChina",            "sector": "Energy"},
            {"ticker": "0386.HK",   "name": "China Petroleum (Sinopec)","sector": "Energy"},
            {"ticker": "0883.HK",   "name": "CNOOC",                 "sector": "Energy"},
            {"ticker": "0002.HK",   "name": "CLP Holdings",          "sector": "Utilities"},
            {"ticker": "0006.HK",   "name": "Power Assets",          "sector": "Utilities"},
            # Industrials
            {"ticker": "1211.HK",   "name": "BYD Company",           "sector": "Consumer Cycl."},
            {"ticker": "2238.HK",   "name": "GAC Group",             "sector": "Consumer Cycl."},
            {"ticker": "1088.HK",   "name": "China Shenhua Energy",  "sector": "Energy"},
            {"ticker": "1816.HK",   "name": "CGN Power",             "sector": "Utilities"},
            {"ticker": "1919.HK",   "name": "COSCO Shipping",        "sector": "Industrials"},
            {"ticker": "0291.HK",   "name": "China Resources Beer",  "sector": "Consumer Def."},
            # Healthcare & Biotech
            {"ticker": "1177.HK",   "name": "Sino Biopharmaceutical","sector": "Healthcare"},
            {"ticker": "2269.HK",   "name": "WuXi Biologics",        "sector": "Healthcare"},
            {"ticker": "2359.HK",   "name": "WuXi AppTec",           "sector": "Healthcare"},
            {"ticker": "6160.HK",   "name": "BeiGene",               "sector": "Healthcare"},
            # Telecom
            {"ticker": "0941.HK",   "name": "China Mobile",          "sector": "Comm. Services"},
            {"ticker": "0762.HK",   "name": "China Unicom",          "sector": "Comm. Services"},
            {"ticker": "0728.HK",   "name": "China Telecom",         "sector": "Comm. Services"},
        ]
    }
}

# In-memory cache for heatmap data to avoid hammering yfinance
_heatmap_cache = {}  # {index_key: {"data": [...], "fetched_at": datetime}}
HEATMAP_CACHE_TTL_MINUTES = 15

@router.get("/prices/heatmap")
async def get_heatmap(index: str = Query("sp500")):
    """
    Returns constituent stock data for the specified index for heatmap rendering.
    Data is cached for 15 minutes to avoid excessive yfinance calls.
    Includes: ticker, name, sector, current_price, change_percent, market_cap (proxy weight).
    """
    index_key = index.lower()
    if index_key not in HEATMAP_INDICES:
        raise HTTPException(status_code=400, detail=f"Unknown index '{index}'. Valid: {list(HEATMAP_INDICES.keys())}")

    # Check cache
    now = datetime.now(timezone.utc)
    cached = _heatmap_cache.get(index_key)
    if cached and (now - cached["fetched_at"]) < timedelta(minutes=HEATMAP_CACHE_TTL_MINUTES):
        print(f"[HEATMAP] Cache hit for {index_key}")
        return {"index": index_key, "label": HEATMAP_INDICES[index_key]["label"], "constituents": cached["data"], "cached": True}

    print(f"[HEATMAP] Fetching live data for {index_key}...")
    constituents = HEATMAP_INDICES[index_key]["constituents"]
    tickers = [c["ticker"] for c in constituents]

    # Build a lookup map
    ticker_meta = {c["ticker"]: c for c in constituents}

    # Fetch sentiment data for these tickers
    sentiment_map = {}
    try:
        conn = get_db_connection_dict()
        cur = conn.cursor()
        cur.execute(f"""
            WITH current_sentiment AS (
                SELECT
                    si.ticker,
                    AVG(si.sentiment_score) AS current_sentiment
                FROM (
                    SELECT si_sub.ticker, si_sub.sentiment_score, a_sub.published_ts
                    FROM {settings.mimir_schema}.mimir_sentiment_impacts si_sub
                    JOIN {settings.mimir_schema}.mimir_raw_articles a_sub ON a_sub.id = si_sub.article_id
                    
                    UNION ALL
                    
                    SELECT sc.ticker, sc.sentiment_score, sc.bucket_ts AS published_ts
                    FROM {settings.mimir_schema}.mimir_social_chatter sc
                ) si
                WHERE si.ticker = ANY(%s)
                  AND si.published_ts > NOW() - INTERVAL '24 hours'
                GROUP BY si.ticker
            ),
            prev_sentiment AS (
                SELECT
                    si.ticker,
                    AVG(si.sentiment_score) AS prev_sentiment
                FROM (
                    SELECT si_sub.ticker, si_sub.sentiment_score, a_sub.published_ts
                    FROM {settings.mimir_schema}.mimir_sentiment_impacts si_sub
                    JOIN {settings.mimir_schema}.mimir_raw_articles a_sub ON a_sub.id = si_sub.article_id
                    
                    UNION ALL
                    
                    SELECT sc.ticker, sc.sentiment_score, sc.bucket_ts AS published_ts
                    FROM {settings.mimir_schema}.mimir_social_chatter sc
                ) si
                WHERE si.ticker = ANY(%s)
                  AND si.published_ts > NOW() - INTERVAL '48 hours'
                  AND si.published_ts <= NOW() - INTERVAL '24 hours'
                GROUP BY si.ticker
            )
            SELECT
                cs.ticker,
                cs.current_sentiment,
                ps.prev_sentiment,
                CASE
                    WHEN ps.prev_sentiment IS NULL OR ABS(ps.prev_sentiment) < 0.0001
                        THEN (cs.current_sentiment - COALESCE(ps.prev_sentiment, 0)) * 100
                    ELSE ((cs.current_sentiment - ps.prev_sentiment) / ABS(ps.prev_sentiment)) * 100
                END AS sentiment_change_percent
            FROM current_sentiment cs
            LEFT JOIN prev_sentiment ps ON cs.ticker = ps.ticker
        """, (tickers, tickers))
        sentiment_rows = cur.fetchall()
        for r in sentiment_rows:
            sentiment_map[r["ticker"]] = {
                "current_sentiment": float(r["current_sentiment"]) if r["current_sentiment"] is not None else None,
                "sentiment_change_percent": float(r["sentiment_change_percent"]) if r["sentiment_change_percent"] is not None else None
            }
        cur.close()
        conn.close()
    except Exception as db_err:
        print(f"[HEATMAP] Database sentiment fetch error: {db_err}")

    # Fetch prices from DB first, yfinance only for stale/missing tickers
    results = []
    try:
        conn = get_db_connection_dict()
        cur = conn.cursor()

        # 1. Check which tickers need a yfinance fetch
        cur.execute(f"""
            SELECT ticker, MAX(scraped_at) as last_scraped
            FROM {settings.mimir_schema}.mimir_hourly_ohlcv
            WHERE ticker = ANY(%s)
            GROUP BY ticker
        """, (tickers,))
        rows = cur.fetchall()
        last_scraped_map = {r["ticker"]: r["last_scraped"] for r in rows}

        stale_tickers = []
        for t in tickers:
            ls = last_scraped_map.get(t)
            if not ls or (now - ls > timedelta(minutes=2)):
                stale_tickers.append(t)

        if stale_tickers:
            print(f"[HEATMAP] Fetching {len(stale_tickers)} stale tickers from yfinance...")
            fetch_and_cache_tickers_concurrently(stale_tickers)

        # 2. Bulk query DB for latest price + 24h-ago price (same pattern as ticker-changes)
        cur.execute(f"""
            WITH latest_prices AS (
                SELECT DISTINCT ON (ticker)
                    ticker,
                    close AS latest_price,
                    timestamp AS latest_ts,
                    volume AS latest_volume
                FROM {settings.mimir_schema}.mimir_hourly_ohlcv
                WHERE ticker = ANY(%s)
                ORDER BY ticker, timestamp DESC
            ),
            prev_prices AS (
                SELECT DISTINCT ON (l.ticker)
                    l.ticker,
                    h.close AS prev_price
                FROM latest_prices l
                JOIN {settings.mimir_schema}.mimir_hourly_ohlcv h ON l.ticker = h.ticker
                WHERE h.timestamp <= l.latest_ts - INTERVAL '24 hours'
                ORDER BY l.ticker, h.timestamp DESC
            )
            SELECT
                l.ticker,
                l.latest_price,
                l.latest_volume,
                COALESCE(p.prev_price, (
                    SELECT close FROM {settings.mimir_schema}.mimir_hourly_ohlcv h2
                    WHERE h2.ticker = l.ticker
                    ORDER BY timestamp ASC LIMIT 1
                )) as prev_price
            FROM latest_prices l
            LEFT JOIN prev_prices p ON l.ticker = p.ticker
        """, (tickers,))
        price_rows = cur.fetchall()
        price_map = {r["ticker"]: r for r in price_rows}
        cur.close()
        conn.close()

        # 3. Build results from DB data
        for ticker_symbol in tickers:
            meta = ticker_meta[ticker_symbol]
            pr = price_map.get(ticker_symbol)
            if not pr:
                continue

            current_price = float(pr["latest_price"])
            prev_price = float(pr["prev_price"]) if pr["prev_price"] is not None else current_price
            volume = int(pr["latest_volume"]) if pr["latest_volume"] else 0

            change_percent = 0.0
            if prev_price > 0:
                change_percent = round(((current_price - prev_price) / prev_price) * 100, 2)

            weight = max(current_price * volume, 1) if volume > 0 else current_price

            sent_info = sentiment_map.get(ticker_symbol, {"current_sentiment": None, "sentiment_change_percent": None})
            results.append({
                "ticker": ticker_symbol,
                "name": meta["name"],
                "sector": meta["sector"],
                "current_price": round(current_price, 4),
                "prev_price": round(prev_price, 4),
                "change_percent": change_percent,
                "volume": volume,
                "weight": weight,
                "current_sentiment": sent_info["current_sentiment"],
                "sentiment_change_percent": sent_info["sentiment_change_percent"],
            })

    except Exception as e:
        print(f"[HEATMAP] DB price fetch error for {index_key}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch heatmap data: {e}")

    if not results:
        raise HTTPException(status_code=503, detail="No data available for this index at the moment.")

    # Cache and return
    _heatmap_cache[index_key] = {"data": results, "fetched_at": now}
    return {
        "index": index_key,
        "label": HEATMAP_INDICES[index_key]["label"],
        "constituents": results,
        "cached": False
    }

@router.get("/prices/search")
async def search_tickers(q: str = Query(..., min_length=1)):
    """
    Search for tickers by name or symbol using yfinance.
    Returns up to 8 matching results with ticker, logo_url, and long_name.
    """
    try:
        from yfinance import Search
        search = Search(q, max_results=8)
        quotes = search.quotes or []
        results = []
        seen = set()
        for quote in quotes:
            ticker = quote.get('symbol', '')
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            logo_url = f"https://finance-logo.perplexity.ai/ticker/{ticker}?format=png&fallback=404&size=50&theme=dark"
            results.append({
                "ticker": ticker,
                "long_name": quote.get('longname') or quote.get('shortname') or ticker,
                "logo_url": logo_url
            })
        return {"results": results, "query": q}
    except Exception as e:
        print(f"[SEARCH] Error searching for '{q}': {e}")
        return {"results": [], "query": q}

# In-memory cache for detailed ticker info to avoid hammering yfinance
_ticker_details_cache = {}  # {ticker_symbol: {"data": ..., "fetched_at": datetime}}
DETAILS_CACHE_TTL_MINUTES = 10

@router.get("/prices/ticker-details/{ticker}")
async def get_ticker_details(ticker: str, nocache: bool = False):
    ticker_symbol = ticker.strip().upper().lstrip('$')
    now = datetime.now(timezone.utc)
    
    # Check cache
    cached = _ticker_details_cache.get(ticker_symbol)
    if cached and not nocache and (now - cached["fetched_at"]) < timedelta(minutes=DETAILS_CACHE_TTL_MINUTES):
        cached_data = cached["data"]
        # If cache has partial/failed details (CEO N/A or description missing), bypass to retry fetching
        if cached_data.get("ceo") != "N/A" and cached_data.get("summary") != "Description not found.":
            print(f"[DETAILS] Cache hit for {ticker_symbol}")
            return cached_data
        
    print(f"[DETAILS] Fetching details from yfinance for {ticker_symbol}...")
    try:
        session = Session(impersonate="chrome", verify=False)
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9"
        })
        ticker_obj = yf.Ticker(ticker_symbol, session=session)
        info = ticker_obj.info
        if not isinstance(info, dict):
            info = {}
    except Exception as e:
        print(f"[DETAILS] yfinance error for {ticker_symbol}: {e}")
        info = {}

    # Look up in HEATMAP_INDICES if sector or name is missing
    lookup_name = None
    lookup_sector = None
    for idx_key, idx_val in HEATMAP_INDICES.items():
        for const in idx_val.get("constituents", []):
            if const["ticker"].upper() == ticker_symbol:
                lookup_name = const["name"]
                lookup_sector = const["sector"]
                break
        if lookup_name:
            break

    # Extract company info
    long_name = info.get("longName") or info.get("shortName") or lookup_name or ticker_symbol
    sector = info.get("sector") or lookup_sector or "N/A"
    industry = info.get("industry") or "N/A"
    country = info.get("country") or "N/A"
    exchange = info.get("exchange") or "N/A"
    summary = info.get("longBusinessSummary") or info.get("description") or "Description not found."

    officers = info.get("companyOfficers", [])
    ceo = "N/A"
    if officers and isinstance(officers, list):
        for officer in officers:
            if "chief executive officer" in officer.get("title", "").lower():
                ceo = officer.get("name", "N/A")
                break
        if ceo == "N/A" and len(officers) > 0:
            ceo = officers[0].get("name", "N/A")
    elif info.get("ceo"):
        ceo = info.get("ceo")

    employees = info.get("fullTimeEmployees") or "N/A"

    # Price data — DB first, yfinance only as fallback
    price = 0.0
    prev_close = 0.0
    open_val = 0.0
    high_val = 0.0
    low_val = 0.0
    volume = 0
    market_cap = info.get("marketCap") or 0  # yfinance metadata, no DB alternative

    # 1. Try DB cache first
    try:
        conn = get_db_connection_dict()
        cur = conn.cursor()
        cur.execute(f"""
            SELECT close, open, high, low, volume
            FROM {settings.mimir_schema}.mimir_hourly_ohlcv
            WHERE ticker = %s
            ORDER BY timestamp DESC
            LIMIT 2
        """, (ticker_symbol,))
        db_rows = cur.fetchall()
        cur.close()
        conn.close()

        if db_rows:
            price = float(db_rows[0]['close'])
            open_val = float(db_rows[0]['open'])
            high_val = float(db_rows[0]['high'])
            low_val = float(db_rows[0]['low'])
            volume = int(db_rows[0]['volume'])
            if len(db_rows) > 1:
                prev_close = float(db_rows[1]['close'])
            else:
                prev_close = price
            print(f"[DETAILS] Using DB price for {ticker_symbol}: {price}")
    except Exception as db_err:
        print(f"[DETAILS] DB price fetch error for {ticker_symbol}: {db_err}")

    # 2. Fall back to yfinance info if DB empty
    if not price or price == 0.0:
        price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("regularMarketPreviousClose") or 0.0
        prev_close = info.get("regularMarketPreviousClose") or price or 0.0
        open_val = info.get("open") or 0.0
        high_val = info.get("dayHigh") or 0.0
        low_val = info.get("dayLow") or 0.0
        volume = info.get("volume") or 0

    # 3. yfinance history fallback
    if not price or price == 0.0:
        try:
            print(f"[DETAILS] Info returned empty price for {ticker_symbol}. Fetching history fallback...")
            hist = ticker_obj.history(period="5d")
            if not hist.empty:
                    latest_row = hist.iloc[-1]
                    price = float(latest_row["Close"])
                    open_val = float(latest_row["Open"]) if open_val == 0.0 else open_val
                    high_val = float(latest_row["High"]) if high_val == 0.0 else high_val
                    low_val = float(latest_row["Low"]) if low_val == 0.0 else low_val
                    volume = int(latest_row["Volume"]) if volume == 0 else volume
                    if len(hist) >= 2:
                        prev_close = float(hist.iloc[-2]["Close"])
                    else:
                        prev_close = price
        except Exception as hist_err:
            print(f"[DETAILS] yfinance history fallback error for {ticker_symbol}: {hist_err}")

    # Calculate change metrics
    if not prev_close or prev_close == 0.0:
        prev_close = price
    change = price - prev_close
    change_percent = (change / prev_close * 100) if prev_close > 0 else 0.0
    
    if open_val == 0.0: open_val = price
    if high_val == 0.0: high_val = price
    if low_val == 0.0: low_val = price
    
    # Calculate market cap if empty and we have shares outstanding
    if not market_cap or market_cap == 0:
        shares = info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")
        if shares and price:
            market_cap = shares * price
            
    pe_ratio = info.get("trailingPE") or info.get("forwardPE")
    div_yield = info.get("dividendYield")
    div_yield_pct = f"{round(div_yield * 100, 2)}%" if div_yield else "N/A"
    
    low_52w = info.get("fiftyTwoWeekLow") or low_val
    high_52w = info.get("fiftyTwoWeekHigh") or high_val
    eps = info.get("trailingEps") or "N/A"
    
    if (not pe_ratio or pe_ratio == "N/A") and isinstance(eps, (int, float)) and eps > 0:
        pe_ratio = round(price / eps, 2)
    if not pe_ratio:
        pe_ratio = "N/A"
        
    # Analyst metrics
    target_low = info.get("targetLowPrice")
    target_mean = info.get("targetMeanPrice")
    target_high = info.get("targetHighPrice")
    
    try:
        targets_dict = ticker_obj.analyst_price_targets
        if targets_dict and isinstance(targets_dict, dict):
            target_low = target_low or targets_dict.get("low")
            target_mean = target_mean or targets_dict.get("mean") or targets_dict.get("median")
            target_high = target_high or targets_dict.get("high")
    except Exception as target_err:
        print(f"[DETAILS] analyst_price_targets error: {target_err}")
        
    target_low = target_low or price
    target_mean = target_mean or price
    target_high = target_high or price
    
    rec_mean = info.get("recommendationMean")
    
    # Recommendations counts fallback
    strong_buy_cnt = 0
    buy_cnt = 0
    hold_cnt = 0
    sell_cnt = 0
    
    try:
        recs_df = ticker_obj.recommendations
        if recs_df is not None and not recs_df.empty:
            latest_row = recs_df.iloc[0]
            strong_buy_cnt = int(latest_row.get("strongBuy") or 0)
            buy_cnt = int(latest_row.get("buy") or 0)
            hold_cnt = int(latest_row.get("hold") or 0)
            sell_cnt = int(latest_row.get("sell") or 0) + int(latest_row.get("strongSell") or 0)
    except Exception as rec_err:
        print(f"[DETAILS] recommendations fetch error: {rec_err}")
        
    analyst_rec = "Hold"
    if rec_mean:
        if rec_mean <= 1.5:
            analyst_rec = "Strong Buy"
        elif rec_mean <= 2.5:
            analyst_rec = "Buy"
        elif rec_mean <= 3.5:
            analyst_rec = "Hold"
        else:
            analyst_rec = "Sell"
    else:
        # Determine from df
        total_bullish = strong_buy_cnt + buy_cnt
        if total_bullish > (hold_cnt + sell_cnt):
            if strong_buy_cnt > buy_cnt:
                analyst_rec = "Strong Buy"
            else:
                analyst_rec = "Buy"
        elif hold_cnt > sell_cnt:
            analyst_rec = "Hold"
        else:
            analyst_rec = "Sell"
            
    analyst_count = info.get("numberOfAnalystOpinions") or (strong_buy_cnt + buy_cnt + hold_cnt + sell_cnt) or 0

    # Fetch recent news/sentiment from database
    db_articles = []
    bullish_view = "No consensus sentiment analysis available at the moment."
    bearish_view = "No consensus sentiment analysis available at the moment."
    sentiment_score = 0.0
    
    try:
        conn = get_db_connection_dict()
        cur = conn.cursor()
        
        # Latest news for this ticker
        cur.execute(f"""
            SELECT DISTINCT a.title, a.link, a.published_ts, a.source_name, si.sentiment_score, a.summary
            FROM {settings.mimir_schema}.mimir_raw_articles a
            JOIN {settings.mimir_schema}.mimir_sentiment_impacts si ON a.id = si.article_id
            WHERE si.ticker = %s
            ORDER BY a.published_ts DESC
            LIMIT 5
        """, (ticker_symbol,))
        rows = cur.fetchall()
        for r in rows:
            db_articles.append({
                "title": r["title"],
                "url": r["link"],
                "published_ts": r["published_ts"].strftime("%b %d, %Y") if r["published_ts"] else "",
                "source": r["source_name"] or "News",
                "sentiment": float(r["sentiment_score"]) if r["sentiment_score"] is not None else 0.0,
                "summary": r["summary"] or ""
            })
            
        # Get overall sentiment (unifying news and social chatter)
        cur.execute("""
            SELECT weighted_score 
            FROM yggdrasil.mimir_weighted_sentiment(p_ticker := %s)
        """, (ticker_symbol,))
        avg_row = cur.fetchone()
        if avg_row and avg_row["weighted_score"] is not None:
            sentiment_score = float(avg_row["weighted_score"])
            
        # Select one positive article summary for Bullish view and one negative for Bearish view
        cur.execute(f"""
            SELECT summary FROM {settings.mimir_schema}.mimir_raw_articles a
            JOIN {settings.mimir_schema}.mimir_sentiment_impacts si ON a.id = si.article_id
            WHERE si.ticker = %s AND si.sentiment_score > 0.1 AND a.summary IS NOT NULL AND a.summary != ''
            ORDER BY si.sentiment_score DESC LIMIT 1
        """, (ticker_symbol,))
        b_row = cur.fetchone()
        if b_row:
            bullish_view = b_row["summary"]
            
        cur.execute(f"""
            SELECT summary FROM {settings.mimir_schema}.mimir_raw_articles a
            JOIN {settings.mimir_schema}.mimir_sentiment_impacts si ON a.id = si.article_id
            WHERE si.ticker = %s AND si.sentiment_score < -0.1 AND a.summary IS NOT NULL AND a.summary != ''
            ORDER BY si.sentiment_score ASC LIMIT 1
        """, (ticker_symbol,))
        bear_row = cur.fetchone()
        if bear_row:
            bearish_view = bear_row["summary"]
            
        cur.close()
        conn.close()
    except Exception as db_err:
        print(f"[DETAILS] Database fetch error for {ticker_symbol}: {db_err}")

    # Fetch peers from the database
    peers_list = []
    try:
        # Find default peers in the same index or sector
        peers_candidates = ["AAPL", "MSFT", "NVDA", "TSLA", "AMD", "QCOM", "INTC"]
        if ticker_symbol in peers_candidates:
            peers_candidates.remove(ticker_symbol)
            
        conn = get_db_connection_dict()
        cur = conn.cursor()
        cur.execute(f"""
            WITH latest_prices AS (
                SELECT DISTINCT ON (ticker)
                    ticker,
                    close AS latest_price,
                    timestamp AS latest_ts
                FROM {settings.mimir_schema}.mimir_hourly_ohlcv
                WHERE ticker = ANY(%s)
                ORDER BY ticker, timestamp DESC
            ),
            prev_prices AS (
                SELECT DISTINCT ON (l.ticker)
                    l.ticker,
                    h.close AS prev_price
                FROM latest_prices l
                JOIN {settings.mimir_schema}.mimir_hourly_ohlcv h ON l.ticker = h.ticker
                WHERE h.timestamp <= l.latest_ts - INTERVAL '24 hours'
                ORDER BY l.ticker, h.timestamp DESC
            )
            SELECT 
                l.ticker,
                l.latest_price,
                COALESCE(p.prev_price, l.latest_price) as prev_price
            FROM latest_prices l
            LEFT JOIN prev_prices p ON l.ticker = p.ticker
        """, (peers_candidates[:4],))
        peer_rows = cur.fetchall()
        cur.close()
        conn.close()
        
        for pr in peer_rows:
            lp = float(pr["latest_price"])
            pp = float(pr["prev_price"])
            pch = ((lp - pp) / pp * 100) if pp > 0 else 0.0
            peers_list.append({
                "ticker": pr["ticker"],
                "price": round(lp, 2),
                "change_percent": round(pch, 2)
            })
    except Exception as peer_err:
        print(f"[DETAILS] Peers fetch error: {peer_err}")

    # Financial statements parsing
    financials_data = {"years": [], "revenue": [], "gross_profit": [], "ebitda": [], "net_income": [], "eps": [], "operating_cash_flow": [], "capex": [], "free_cash_flow": []}
    try:
        fin = ticker_obj.income_stmt
        cf = ticker_obj.cash_flow
        
        if fin is not None and not fin.empty:
            years = [col.strftime('%Y-%m-%d') if hasattr(col, 'strftime') else str(col) for col in fin.columns]
            financials_data["years"] = years
            
            def get_row(df, keys):
                for k in keys:
                    # Case insensitive lookup
                    for index_val in df.index:
                        if str(index_val).strip().lower() == k.strip().lower():
                            return df.loc[index_val]
                return None
                
            rev_row = get_row(fin, ['Total Revenue', 'Revenue'])
            financials_data["revenue"] = [float(v) if pd.notna(v) else 0.0 for v in rev_row] if rev_row is not None else [0.0]*len(years)
            
            gp_row = get_row(fin, ['Gross Profit'])
            financials_data["gross_profit"] = [float(v) if pd.notna(v) else 0.0 for v in gp_row] if gp_row is not None else [0.0]*len(years)
            
            ebitda_row = get_row(fin, ['EBITDA'])
            financials_data["ebitda"] = [float(v) if pd.notna(v) else 0.0 for v in ebitda_row] if ebitda_row is not None else [0.0]*len(years)
            
            ni_row = get_row(fin, ['Net Income', 'Net Income Common Stockholders'])
            financials_data["net_income"] = [float(v) if pd.notna(v) else 0.0 for v in ni_row] if ni_row is not None else [0.0]*len(years)
            
            eps_row = get_row(fin, ['Diluted EPS', 'Basic EPS'])
            financials_data["eps"] = [float(v) if pd.notna(v) else 0.0 for v in eps_row] if eps_row is not None else [0.0]*len(years)

        if cf is not None and not cf.empty:
            ocf_row = get_row(cf, ['Operating Cash Flow', 'Cash Flow From Operating Activities'])
            financials_data["operating_cash_flow"] = [float(v) if pd.notna(v) else 0.0 for v in ocf_row] if ocf_row is not None else [0.0]*len(financials_data["years"])
            
            capex_row = get_row(cf, ['Capital Expenditure', 'Capital Expenditures'])
            financials_data["capex"] = [float(v) if pd.notna(v) else 0.0 for v in capex_row] if capex_row is not None else [0.0]*len(financials_data["years"])
            
            fcf_row = get_row(cf, ['Free Cash Flow'])
            financials_data["free_cash_flow"] = [float(v) if pd.notna(v) else 0.0 for v in fcf_row] if fcf_row is not None else [0.0]*len(financials_data["years"])
    except Exception as fin_err:
        print(f"[DETAILS] Financial parsing error: {fin_err}")

    # Final payload
    details_payload = {
        "ticker": ticker_symbol,
        "long_name": long_name,
        "sector": sector,
        "industry": industry,
        "country": country,
        "exchange": exchange,
        "summary": summary,
        "ceo": ceo,
        "employees": employees,
        "price": round(price, 2),
        "change": round(change, 2),
        "change_percent": round(change_percent, 2),
        "prev_close": round(prev_close, 2),
        "open": round(open_val, 2),
        "high": round(high_val, 2),
        "low": round(low_val, 2),
        "volume": volume,
        "market_cap": market_cap,
        "pe_ratio": pe_ratio,
        "dividend_yield": div_yield_pct,
        "fifty_two_week_low": round(low_52w, 2),
        "fifty_two_week_high": round(high_52w, 2),
        "eps": eps,
        "analyst_recommendation": analyst_rec,
        "analyst_count": analyst_count,
        "analyst_bearish": sell_cnt,
        "analyst_neutral": hold_cnt,
        "analyst_bullish": strong_buy_cnt + buy_cnt,
        "target_low": round(target_low, 2),
        "target_mean": round(target_mean, 2),
        "target_high": round(target_high, 2),
        "sentiment_score": round(sentiment_score, 2),
        "bullish_view": bullish_view,
        "bearish_view": bearish_view,
        "recent_articles": db_articles,
        "peers": peers_list,
        "financials": financials_data
    }
    
    # Store in cache only if successfully fetched positive price
    if details_payload["price"] > 0.0:
        _ticker_details_cache[ticker_symbol] = {"data": details_payload, "fetched_at": now}
    return details_payload

