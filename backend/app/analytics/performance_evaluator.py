# backend/app/analytics/performance_evaluator.py
import os
import sys
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Adjust path to import backend
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()

def get_ticker_hold_days(cur, ticker: str) -> int:
    """Retrieves optimal hold days for a ticker, defaulting to 10."""
    try:
        cur.execute(f"""
            SELECT optimal_hold_days 
            FROM {settings.mimir_schema}.mimir_ticker_parameters 
            WHERE ticker = %s
        """, (ticker,))
        row = cur.fetchone()
        return int(row[0]) if row else 10
    except Exception:
        return 10

def fetch_eval_price_db(cur, ticker: str, target_date) -> float:
    """Tries to query close price closest to target_date in the database."""
    try:
        cur.execute(f"""
            SELECT close 
            FROM {settings.mimir_schema}.v_mimir_daily_ohlcv
            WHERE ticker = %s AND date >= %s
            ORDER BY date ASC
            LIMIT 1
        """, (ticker, target_date))
        row = cur.fetchone()
        return float(row[0]) if row else None
    except Exception as e:
        print(f"[EVAL] DB price lookup error for {ticker}: {e}")
        return None

def fetch_eval_price_online(ticker: str, target_date) -> float:
    """Tries to download close price from yfinance as a fallback."""
    try:
        start_str = target_date.strftime('%Y-%m-%d')
        end_date = target_date + timedelta(days=4)
        end_str = end_date.strftime('%Y-%m-%d')
        
        print(f"[EVAL] Fetching fallback price online for {ticker} from {start_str} to {end_str}...")
        df = yf.download(ticker, start=start_str, end=end_str, progress=False)
        if not df.empty:
            # Get close price of the first available row
            # YFinance returns a multi-index or single index depending on pandas/yf version
            if 'Close' in df.columns:
                close_col = df['Close']
                # Check if it is a Series or DataFrame
                if isinstance(close_col, pd.DataFrame):
                    val = close_col.iloc[0, 0]
                else:
                    val = close_col.iloc[0]
                return float(val)
    except Exception as e:
        print(f"[EVAL] YFinance fallback failed for {ticker}: {e}")
    return None

def evaluate_past_signals(conn=None) -> int:
    """
    Finds mature unevaluated trade signals in the database,
    resolves their closing prices at holding period maturity,
    and updates their P&L performance metrics.
    """
    should_close_conn = False
    if conn is None:
        conn = get_db_connection()
        should_close_conn = True
        
    cur = conn.cursor()
    
    try:
        # Fetch all signals that have not been evaluated yet
        cur.execute(f"""
            SELECT id, ticker, signal_type, trigger_price, status, created_at 
            FROM {settings.mimir_schema}.mimir_trade_signals
            WHERE evaluation_status IS NULL
            ORDER BY created_at ASC
        """)
        signals = cur.fetchall()
        
        if not signals:
            cur.close()
            if should_close_conn:
                conn.close()
            return 0
            
        print(f"[EVAL] Scanning {len(signals)} unevaluated trade signals...")
        
        now = datetime.now(timezone.utc)
        evaluated_count = 0
        
        for sid, ticker, signal_type, trigger_price, status, created_at in signals:
            # Look up configured holding period
            hold_days = get_ticker_hold_days(cur, ticker)
            
            # Ensure trade signal is mature
            target_dt = created_at + timedelta(days=hold_days)
            if target_dt > now:
                # Signal is not mature yet; skip
                continue
                
            target_date = target_dt.date()
            trigger_price = float(trigger_price)
            
            # 1. Fetch close price at maturity (DB first)
            eval_price = fetch_eval_price_db(cur, ticker, target_date)
            
            # 2. Fallback to YFinance if database has no record for that date
            if eval_price is None:
                eval_price = fetch_eval_price_online(ticker, target_date)
                
            if eval_price is None:
                print(f"[EVAL] [Warning] Could not resolve evaluation price for {ticker} (Target Date: {target_date}). Skipping.")
                continue
                
            # 3. Calculate PnL %
            if signal_type.upper() == 'BUY':
                pnl_pct = ((eval_price - trigger_price) / trigger_price) * 100.0
            else:  # SELL
                pnl_pct = ((trigger_price - eval_price) / trigger_price) * 100.0
                
            # Classify success status
            # Successful if PnL is positive (i.e. price rose for BUY, or fell for SELL)
            eval_status = 'SUCCESSFUL' if pnl_pct > 0.0 else 'FAILED'
            
            # 4. Update trade signal record
            cur.execute(f"""
                UPDATE {settings.mimir_schema}.mimir_trade_signals
                SET evaluation_price = %s,
                    evaluation_pnl_pct = %s,
                    evaluation_status = %s,
                    evaluated_at = NOW()
                WHERE id = %s
            """, (eval_price, pnl_pct, eval_status, sid))
            
            print(f"[EVAL] Signal #{sid} ({ticker} {signal_type}): Trigger {trigger_price:.2f} -> Eval {eval_price:.2f} | PnL: {pnl_pct:.2f}% | Status: {eval_status}")
            evaluated_count += 1
            
        conn.commit()
        cur.close()
        if should_close_conn:
            conn.close()
        return evaluated_count
        
    except Exception as e:
        conn.rollback()
        print(f"[EVAL] Error during performance evaluation cycle: {e}")
        if cur:
            cur.close()
        if should_close_conn:
            conn.close()
        return 0

if __name__ == "__main__":
    print("Running manual evaluation cycle...")
    count = evaluate_past_signals()
    print(f"Evaluation cycle finished. Evaluated {count} signals.")
