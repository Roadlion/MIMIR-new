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

    Also checks if target_price or stop_loss was hit intra-period
    using daily OHLCV high/low — more accurate than end-of-period only.
    """
    should_close_conn = False
    if conn is None:
        conn = get_db_connection()
        should_close_conn = True

    cur = conn.cursor()

    try:
        # Fetch all signals that have not been evaluated yet
        cur.execute(f"""
            SELECT id, ticker, signal_type, trigger_price, target_price, stop_loss, status, created_at
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

        for sid, ticker, signal_type, trigger_price, target_price, stop_loss, status, created_at in signals:
            hold_days = get_ticker_hold_days(cur, ticker)

            # Ensure trade signal is mature
            target_dt = created_at + timedelta(days=hold_days)
            if target_dt > now:
                continue

            trigger_price = float(trigger_price)
            target_price = float(target_price) if target_price else None
            stop_loss = float(stop_loss) if stop_loss else None

            entry_date = created_at.date()
            exit_date = target_dt.date()

            # ── Intra-period target/stop check using daily OHLCV ──────────────
            # Check if price HIT the target or stop DURING the hold period.
            # This is more accurate than just looking at end-of-period price.
            hit_target = False
            hit_stop = False
            early_exit_pnl = None

            try:
                cur.execute(f"""
                    SELECT date, high, low, close
                    FROM {settings.mimir_schema}.v_mimir_daily_ohlcv
                    WHERE ticker = %s AND date >= %s AND date <= %s
                    ORDER BY date ASC
                """, (ticker, entry_date, exit_date))
                ohlcv_rows = cur.fetchall()

                for _date, high, low, close in ohlcv_rows:
                    high = float(high) if high else None
                    low = float(low) if low else None

                    if signal_type.upper() == 'BUY':
                        if target_price and high and high >= target_price:
                            hit_target = True
                            early_exit_pnl = ((target_price - trigger_price) / trigger_price) * 100.0
                            break
                        if stop_loss and low and low <= stop_loss:
                            hit_stop = True
                            early_exit_pnl = ((stop_loss - trigger_price) / trigger_price) * 100.0
                            break
                    else:  # SELL / SHORT
                        if target_price and low and low <= target_price:
                            hit_target = True
                            early_exit_pnl = ((trigger_price - target_price) / trigger_price) * 100.0
                            break
                        if stop_loss and high and high >= stop_loss:
                            hit_stop = True
                            early_exit_pnl = ((trigger_price - stop_loss) / trigger_price) * 100.0
                            break
            except Exception as ohlcv_err:
                print(f"[EVAL] Intra-period OHLCV check failed for {ticker}: {ohlcv_err}")

            # ── Determine final PnL ──────────────────────────────────────────
            if hit_target or hit_stop:
                pnl_pct = early_exit_pnl
                eval_price = trigger_price * (1 + pnl_pct / 100.0)
                exit_reason = "TARGET_HIT" if hit_target else "STOP_HIT"
            else:
                # No intra-period exit — evaluate at hold-period maturity
                eval_price = fetch_eval_price_db(cur, ticker, exit_date)
                if eval_price is None:
                    eval_price = fetch_eval_price_online(ticker, target_dt)
                if eval_price is None:
                    print(f"[EVAL] [Warning] Could not resolve evaluation price for {ticker} (Target Date: {exit_date}). Skipping.")
                    continue

                if signal_type.upper() == 'BUY':
                    pnl_pct = ((eval_price - trigger_price) / trigger_price) * 100.0
                else:
                    pnl_pct = ((trigger_price - eval_price) / trigger_price) * 100.0
                exit_reason = "MATURITY"

            # Successful: target hit OR positive end-of-period PnL
            eval_status = 'SUCCESSFUL' if (hit_target or pnl_pct > 0.0) else 'FAILED'

            cur.execute(f"""
                UPDATE {settings.mimir_schema}.mimir_trade_signals
                SET evaluation_price     = %s,
                    evaluation_pnl_pct   = %s,
                    evaluation_status    = %s,
                    evaluated_at         = NOW()
                WHERE id = %s
            """, (eval_price, pnl_pct, eval_status, sid))

            print(
                f"[EVAL] Signal #{sid} ({ticker} {signal_type}): "
                f"Trigger {trigger_price:.2f} → Eval {eval_price:.2f} | "
                f"PnL: {pnl_pct:.2f}% | Exit: {exit_reason} | Status: {eval_status}"
            )
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
