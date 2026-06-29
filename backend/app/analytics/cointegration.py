import pandas as pd
import numpy as np
from ..database import get_db_connection
from ..config import get_settings

settings = get_settings()

# ponytail: curated pairs — dynamic cointegration scanning is noisy at this scale
NICHE_PAIRS = [
    # Grains
    ("CORN", "WEAT"),
    ("WEAT", "SOYB"),
    ("CORN", "SOYB"),
    # Shipping
    ("BDRY", "SBLK"),
    ("BDRY", "GOGL"),
    # Nuclear / Energy
    ("URA", "NLR"),
    ("XLE", "XOP"),
    # Gold miners
    ("GDX", "GDXJ"),
    # Battery metals
    ("COPX", "LIT"),
]


def _fetch_daily_closes(ticker, period_days=365):
    """Fetch daily close prices from mimir_hourly_ohlcv, fall back to yfinance."""
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=period_days)
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"""
            SELECT DISTINCT ON (timestamp::date)
                timestamp::date AS date, close
            FROM {settings.mimir_schema}.mimir_hourly_ohlcv
            WHERE ticker = %s AND timestamp >= %s
            ORDER BY timestamp::date, timestamp DESC
        """, (ticker, cutoff))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        if rows:
            df = pd.DataFrame(rows, columns=["date", "close"])
            df = df.set_index("date").sort_index()
            return df
    except Exception as e:
        print(f"[cointegration] DB fetch failed for {ticker}: {e}")

    # Fallback: yfinance
    import time
    import random
    time.sleep(random.uniform(1.0, 2.5))  # Prevents Yahoo Finance rate-limiting when scanning multiple pairs
    try:
        import yfinance as yf
        import requests
        session = requests.Session()
        session.verify = False
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        
        for attempt in range(3):
            try:
                data = yf.download(ticker, period=f"{period_days}d", progress=False, session=session)["Close"]
                if data is not None and not data.empty:
                    if isinstance(data, pd.DataFrame):
                        data = data.iloc[:, 0]
                    return data.to_frame("close")
            except Exception as ex:
                if attempt == 2:
                    raise ex
                time.sleep(2 ** (attempt + 1))
    except Exception as e:
        print(f"[cointegration] yfinance fallback failed for {ticker}: {e}")
    return None


def calculate_z_score(ticker1, ticker2, period_days=365):
    """Calculate current z-score of the T1/T2 spread ratio using cached OHLCV data."""
    data1 = _fetch_daily_closes(ticker1, period_days)
    data2 = _fetch_daily_closes(ticker2, period_days)
    if data1 is None or data2 is None or data1.empty or data2.empty:
        return None

    df = pd.concat([data1["close"], data2["close"]], axis=1, join="inner").dropna()
    df.columns = [ticker1, ticker2]

    if len(df) < 30:
        return None

    df["Spread"] = df[ticker1] / df[ticker2]
    mean_spread = df["Spread"].mean()
    std_spread = df["Spread"].std()
    if std_spread == 0:
        return None

    current_spread = df["Spread"].iloc[-1]
    z_score = (current_spread - mean_spread) / std_spread

    if z_score > 2.0:
        signal = f"SHORT {ticker1}, LONG {ticker2}"
    elif z_score < -2.0:
        signal = f"LONG {ticker1}, SHORT {ticker2}"
    else:
        signal = "WAIT"

    return {
        "pair": f"{ticker1} / {ticker2}",
        "ticker1": ticker1,
        "ticker2": ticker2,
        "z_score": round(float(z_score), 2),
        "mean_spread": round(float(mean_spread), 4),
        "current_spread": round(float(current_spread), 4),
        "signal": signal,
        "status": "OPEN" if signal != "WAIT" else "CLOSED",
    }


def scan_niche_opportunities():
    """Scan all NICHE_PAIRS for stat-arb opportunities."""
    opportunities = []
    for t1, t2 in NICHE_PAIRS:
        res = calculate_z_score(t1, t2)
        if res:
            opportunities.append(res)
    return opportunities
