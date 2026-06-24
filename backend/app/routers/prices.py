# backend/app/routers/prices.py
from fastapi import APIRouter, Query, HTTPException
from typing import List, Optional
from datetime import datetime, timedelta, timezone
import yfinance as yf
import pandas as pd
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
    "EURUSD=X", "USDJPY=X", "AAPL", "MSFT", "NVDA", "TSLA", "^SET50.BK",
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "BNB-USD", "ADA-USD"
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

    results = []
    try:
        # Use yfinance download for batch efficiency
        session = Session(impersonate="chrome", verify=False)
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9"
        })

        # Download daily data to get today vs yesterday close
        # yfinance 1.4.x default: multi_level_index=True, group_by='column'
        # Result columns: (field, ticker) e.g. ('Close', 'AAPL')
        ticker_str = " ".join(tickers)
        is_single = len(tickers) == 1
        df = yf.download(ticker_str, period="5d", interval="1d",
                         auto_adjust=True, session=session, progress=False,
                         multi_level_index=not is_single)

        for ticker_symbol in tickers:
            meta = ticker_meta[ticker_symbol]
            try:
                if is_single:
                    # Single ticker: flat columns (Close, Volume, ...)
                    ticker_df = df.dropna(subset=["Close"]) if not df.empty else None
                else:
                    # Multi-ticker: columns are (field, ticker)
                    if "Close" not in df.columns.get_level_values(0):
                        continue
                    close_col = df["Close"]
                    if ticker_symbol not in close_col.columns:
                        continue
                    vol_col = df["Volume"] if "Volume" in df.columns.get_level_values(0) else None

                    # Build per-ticker dataframe
                    ticker_data = {"Close": close_col[ticker_symbol]}
                    if vol_col is not None and ticker_symbol in vol_col.columns:
                        ticker_data["Volume"] = vol_col[ticker_symbol]
                    ticker_df = pd.DataFrame(ticker_data).dropna(subset=["Close"])

                if ticker_df is None or ticker_df.empty:
                    continue

                current_price = float(ticker_df["Close"].iloc[-1])
                prev_price = float(ticker_df["Close"].iloc[-2]) if len(ticker_df) >= 2 else current_price
                volume = int(ticker_df["Volume"].iloc[-1]) if "Volume" in ticker_df.columns and not pd.isna(ticker_df["Volume"].iloc[-1]) else 0

                change_percent = 0.0
                if prev_price > 0:
                    change_percent = round(((current_price - prev_price) / prev_price) * 100, 2)

                # Use volume * price as a proxy for market cap weight (for treemap sizing)
                weight = max(current_price * volume, 1) if volume > 0 else current_price

                results.append({
                    "ticker": ticker_symbol,
                    "name": meta["name"],
                    "sector": meta["sector"],
                    "current_price": round(current_price, 4),
                    "prev_price": round(prev_price, 4),
                    "change_percent": change_percent,
                    "volume": volume,
                    "weight": weight,
                })
            except Exception as e:
                print(f"[HEATMAP] Error processing {ticker_symbol}: {e}")
                continue

    except Exception as e:
        print(f"[HEATMAP] Batch download error for {index_key}: {e}")
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
