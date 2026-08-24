# scripts/test_earnings_fetcher.py
import sys
import os
import yfinance as yf
import pandas as pd
from datetime import datetime, date

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()

def get_next_earnings_date(ticker: str) -> str:
    """Fetches the next scheduled earnings date for a ticker as YYYY-MM-DD using yfinance."""
    try:
        t = yf.Ticker(ticker)
        
        # 1. Try calendar dict/dataframe
        try:
            cal = t.calendar
            if isinstance(cal, dict) and "Earnings Date" in cal:
                ed_list = cal["Earnings Date"]
                if ed_list and len(ed_list) > 0:
                    d = ed_list[0]
                    if isinstance(d, (datetime, date)):
                        return d.strftime("%Y-%m-%d")
                    return str(d)[:10]
            elif isinstance(cal, pd.DataFrame) and not cal.empty:
                if "Earnings Date" in cal.columns:
                    val = cal["Earnings Date"].iloc[0]
                    return str(val)[:10]
        except Exception:
            pass

        # 2. Try earnings_dates dataframe
        try:
            ed = t.earnings_dates
            if ed is not None and not ed.empty:
                now = pd.Timestamp.now(tz=ed.index.tz if ed.index.tz else None)
                future = ed[ed.index >= now]
                if not future.empty:
                    next_dt = future.index[-1]
                    return next_dt.strftime("%Y-%m-%d")
        except Exception:
            pass

    except Exception as e:
        print(f"[EARNINGS_FETCHER] Error for {ticker}: {e}")

    return "N/A"

def test_fetch_tickers():
    test_tickers = ["NVDA", "AAPL", "MSFT", "ASTS", "SMR", "KTOS", "UUUU"]
    print("[TEST] Fetching next scheduled earnings dates via yfinance...\n")
    for ticker in test_tickers:
        dt_str = get_next_earnings_date(ticker)
        print(f" -> Ticker: {ticker:<6} | Next Earnings Date: {dt_str}")

if __name__ == "__main__":
    test_fetch_tickers()
