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
from backend.app.utils.ticker_validator import is_yfinance_compatible, filter_yfinance_tickers

settings = get_settings()

try:
    yf_cache.get_cookie_cache().dummy = True
except Exception:
    pass

# Create a shared curl_cffi session with SSL verification disabled
session = Session(impersonate="chrome")
session.verify = False

def ensure_schema_migration(cur, conn):
    """Ensure all enriched fundamental and valuation columns exist in mimir_asset_fundamentals."""
    cols_to_add = [
        ("ev_ebitda", "DOUBLE PRECISION"),
        ("price_to_sales", "DOUBLE PRECISION"),
        ("price_to_book", "DOUBLE PRECISION"),
        ("free_cash_flow", "DOUBLE PRECISION"),
        ("fcf_yield", "DOUBLE PRECISION"),
        ("roe", "DOUBLE PRECISION"),
        ("dcf_intrinsic_value", "DOUBLE PRECISION"),
        ("valuation_status", "VARCHAR(50)"),
        ("earnings_summary", "TEXT")
    ]
    for col_name, col_type in cols_to_add:
        try:
            cur.execute(f"""
                ALTER TABLE {settings.mimir_schema}.mimir_asset_fundamentals 
                ADD COLUMN IF NOT EXISTS {col_name} {col_type};
            """)
        except Exception as e:
            print(f"[FUNDAMENTALS MIGRATION] Column {col_name} check/add warning: {e}")
    conn.commit()

def calculate_dcf(free_cash_flow, eps_growth, current_price, shares_outstanding=None, market_cap=None):
    """
    Deterministic 2-stage DCF calculation.
    FCF growth for 5 years, discounted at WACC 9.0%, terminal growth 2.5%.
    """
    if current_price is None or current_price <= 0:
        return None, "FAIRLY_VALUED"
        
    # Estimate starting FCF per share
    fcf_per_share = None
    if free_cash_flow and shares_outstanding and shares_outstanding > 0:
        fcf_per_share = free_cash_flow / shares_outstanding
    elif free_cash_flow and market_cap and market_cap > 0:
        fcf_per_share = (free_cash_flow / market_cap) * current_price
    elif free_cash_flow and free_cash_flow > 0:
        fcf_per_share = current_price * 0.05  # reasonable fallback assumption (5% FCF yield)
    else:
        # Estimate FCF per share from current price assuming 4% FCF yield
        fcf_per_share = current_price * 0.04

    wacc = 0.09
    terminal_g = 0.025
    g = eps_growth if (eps_growth is not None and -0.30 <= eps_growth <= 0.50) else 0.08
    
    pv_fcf = 0.0
    fcf = fcf_per_share
    for t in range(1, 6):
        fcf *= (1.0 + g)
        pv_fcf += fcf / ((1.0 + wacc) ** t)
        
    terminal_val = (fcf * (1.0 + terminal_g)) / (wacc - terminal_g)
    pv_terminal = terminal_val / ((1.0 + wacc) ** 5)
    
    intrinsic_value = round(pv_fcf + pv_terminal, 2)
    
    if intrinsic_value > current_price * 1.15:
        val_status = "UNDERVALUED"
    elif intrinsic_value < current_price * 0.85:
        val_status = "OVERVALUED"
    else:
        val_status = "FAIRLY_VALUED"
        
    return intrinsic_value, val_status

def fetch_and_cache_fundamentals(ticker_symbol: str, cur, conn, force=False):
    ticker_symbol = ticker_symbol.strip().lstrip('$').upper()

    # Guard: skip non-equity tickers (ISINs, structured products) before touching yfinance
    if not is_yfinance_compatible(ticker_symbol):
        print(f"[FUNDAMENTALS] {ticker_symbol} skipped — not a yfinance-compatible equity ticker.")
        return "skipped"

    if not force:
        cur.execute(f"""
            SELECT pe_ratio, debt_to_equity, eps_growth, operating_margin, updated_at 
            FROM {settings.mimir_schema}.mimir_asset_fundamentals 
            WHERE ticker = %s
        """, (ticker_symbol,))
        row = cur.fetchone()
        
        if row and row[4]:
            updated_at = row[4]
            if datetime.now(timezone.utc) - updated_at < timedelta(days=1):
                print(f"[FUNDAMENTALS] {ticker_symbol} is fresh (updated at {updated_at}). Skipping.")
                return "skipped"
            
    print(f"[FUNDAMENTALS] Fetching {ticker_symbol} from yfinance...")
    try:
        ticker = yf.Ticker(ticker_symbol, session=session)
        info = ticker.info or {}
        if not info:
            print(f" [!] Empty info dictionary returned for {ticker_symbol}")
            return "failed"
            
        pe_ratio = info.get("trailingPE") or info.get("forwardPE")
        debt_to_equity = info.get("debtToEquity")
        eps_growth = info.get("earningsGrowth") or info.get("earningsQuarterlyGrowth")
        operating_margin = info.get("operatingMargins")
        
        ev_ebitda = info.get("enterpriseToEbitda")
        price_to_sales = info.get("priceToSalesTrailing12Months")
        price_to_book = info.get("priceToBook")
        free_cash_flow = info.get("freeCashflow") or info.get("operatingCashflow")
        market_cap = info.get("marketCap")
        current_price = info.get("currentPrice") or info.get("previousClose")
        roe = info.get("returnOnEquity")
        shares_outstanding = info.get("sharesOutstanding")
        
        fcf_yield = (free_cash_flow / market_cap) if (free_cash_flow and market_cap and market_cap > 0) else None
        
        dcf_intrinsic_value, valuation_status = calculate_dcf(
            free_cash_flow, eps_growth, current_price, shares_outstanding, market_cap
        )
        
        # Convert types safely
        pe_ratio = float(pe_ratio) if pe_ratio is not None else None
        debt_to_equity = float(debt_to_equity) if debt_to_equity is not None else None
        eps_growth = float(eps_growth) if eps_growth is not None else None
        operating_margin = float(operating_margin) if operating_margin is not None else None
        ev_ebitda = float(ev_ebitda) if ev_ebitda is not None else None
        price_to_sales = float(price_to_sales) if price_to_sales is not None else None
        price_to_book = float(price_to_book) if price_to_book is not None else None
        free_cash_flow = float(free_cash_flow) if free_cash_flow is not None else None
        fcf_yield = float(fcf_yield) if fcf_yield is not None else None
        roe = float(roe) if roe is not None else None
        
        earnings_summary = (
            f"P/E: {pe_ratio or 'N/A'}, EV/EBITDA: {ev_ebitda or 'N/A'}, "
            f"EPS Growth: {f'{eps_growth*100:.1f}%' if eps_growth else 'N/A'}, "
            f"Op Margin: {f'{operating_margin*100:.1f}%' if operating_margin else 'N/A'}, "
            f"DCF Value: ${dcf_intrinsic_value or 'N/A'} ({valuation_status})"
        )
        
        cur.execute(f"""
            INSERT INTO {settings.mimir_schema}.mimir_asset_fundamentals (
                ticker, pe_ratio, debt_to_equity, eps_growth, operating_margin,
                ev_ebitda, price_to_sales, price_to_book, free_cash_flow, fcf_yield, roe,
                dcf_intrinsic_value, valuation_status, earnings_summary, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (ticker) DO UPDATE 
            SET pe_ratio = EXCLUDED.pe_ratio,
                debt_to_equity = EXCLUDED.debt_to_equity,
                eps_growth = EXCLUDED.eps_growth,
                operating_margin = EXCLUDED.operating_margin,
                ev_ebitda = EXCLUDED.ev_ebitda,
                price_to_sales = EXCLUDED.price_to_sales,
                price_to_book = EXCLUDED.price_to_book,
                free_cash_flow = EXCLUDED.free_cash_flow,
                fcf_yield = EXCLUDED.fcf_yield,
                roe = EXCLUDED.roe,
                dcf_intrinsic_value = EXCLUDED.dcf_intrinsic_value,
                valuation_status = EXCLUDED.valuation_status,
                earnings_summary = EXCLUDED.earnings_summary,
                updated_at = NOW();
        """, (
            ticker_symbol, pe_ratio, debt_to_equity, eps_growth, operating_margin,
            ev_ebitda, price_to_sales, price_to_book, free_cash_flow, fcf_yield, roe,
            dcf_intrinsic_value, valuation_status, earnings_summary
        ))
        conn.commit()
        print(f" [ok] {ticker_symbol} -> PE: {pe_ratio}, DCF Val: ${dcf_intrinsic_value} ({valuation_status}), FCF Yield: {f'{fcf_yield*100:.1f}%' if fcf_yield else 'N/A'}")
        return "fetched"
    except Exception as e:
        conn.rollback()
        print(f" [error] Failed to fetch fundamentals for {ticker_symbol}: {e}")
        return "failed"

def main():
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        ensure_schema_migration(cur, conn)
        
        # Load all active dynamic tickers from database
        cur.execute(f"SELECT DISTINCT ticker FROM {settings.mimir_schema}.mimir_dynamic_tickers WHERE ticker IS NOT NULL")
        raw_tickers = [row[0].strip().upper() for row in cur.fetchall()]
        if not raw_tickers:
            raw_tickers = ["AAPL", "NVDA", "MSFT", "TSLA", "AMZN", "GOOGL", "META", "SPY", "QQQ"]

        # Filter out ISIN-format tickers (structured products, bonds) that yfinance can't handle
        tickers = filter_yfinance_tickers(raw_tickers)
        filtered_out = len(raw_tickers) - len(tickers)
        print(f"[FUNDAMENTALS] Loaded {len(raw_tickers)} tickers ({filtered_out} non-equity filtered out). Processing {len(tickers)} valid tickers.")
        
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

