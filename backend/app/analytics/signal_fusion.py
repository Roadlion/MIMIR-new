# backend/app/analytics/signal_fusion.py
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

from ..database import get_db_connection
from ..config import get_settings
from .technical_analysis import analyze_technical_indicators
from ..routers.prices import DEFAULT_TICKERS

settings = get_settings()

def get_recent_prices(ticker: str, days: int = 120, conn=None) -> pd.DataFrame:
    """Fetches recent daily prices for a ticker from the database."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    
    start_date = (datetime.now() - timedelta(days=days)).date()
    
    sql = f"""
        SELECT date, open, high, low, close, volume
        FROM {settings.mimir_schema}.v_mimir_daily_ohlcv
        WHERE ticker = %s AND date >= %s
        ORDER BY date ASC
    """
    try:
        cur.execute(sql, (ticker.strip().upper(), start_date))
        rows = cur.fetchall()
        if not rows:
            return pd.DataFrame()
            
        df = pd.DataFrame(rows, columns=['date', 'open', 'high', 'low', 'close', 'volume'])
        df['date'] = pd.to_datetime(df['date'])
        df.set_index('date', inplace=True)
        # Convert numeric columns
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        return df
    except Exception as e:
        print(f"[SIGNAL_FUSION] Error fetching prices for {ticker}: {e}")
        return pd.DataFrame()
    finally:
        cur.close()
        if close_conn:
            conn.close()

def get_recent_sentiment(ticker: str, days: int = 5, conn=None) -> Optional[float]:
    """Fetches the average sentiment score for a ticker over the last N days."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    
    start_date = (datetime.now() - timedelta(days=days)).date()
    
    sql = f"""
        SELECT AVG(si.sentiment_score)
        FROM {settings.mimir_schema}.mimir_sentiment_impacts si
        JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
        WHERE si.ticker = %s AND (a.published_ts AT TIME ZONE 'UTC')::date >= %s
    """
    try:
        cur.execute(sql, (ticker.strip().upper(), start_date))
        val = cur.fetchone()[0]
        return float(val) if val is not None else None
    except Exception as e:
        print(f"[SIGNAL_FUSION] Error fetching sentiment for {ticker}: {e}")
        return None
    finally:
        cur.close()
        if close_conn:
            conn.close()

def check_duplicate_signal(ticker: str, signal_type: str, conn=None) -> bool:
    """Checks if there's already a PENDING signal of the same type for this ticker."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    
    sql = f"""
        SELECT 1 FROM {settings.mimir_schema}.mimir_trade_signals
        WHERE ticker = %s AND signal_type = %s AND status = 'PENDING'
        LIMIT 1
    """
    try:
        cur.execute(sql, (ticker, signal_type))
        return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        cur.close()
        if close_conn:
            conn.close()

def insert_trade_signal(ticker: str, signal_type: str, price: float, rsi: float, sentiment: float, support: float, resistance: float, reason: str, conn=None) -> bool:
    """Inserts a new trade signal into the database."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    
    sql = f"""
        INSERT INTO {settings.mimir_schema}.mimir_trade_signals 
        (ticker, signal_type, trigger_price, rsi_value, sentiment_score, support_level, resistance_level, reason, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'PENDING')
    """
    try:
        cur.execute(sql, (ticker, signal_type, price, rsi, sentiment, support, resistance, reason))
        conn.commit()
        return True
    except Exception as e:
        print(f"[SIGNAL_FUSION] Error inserting signal: {e}")
        return False
    finally:
        cur.close()
        if close_conn:
            conn.close()

def get_cached_fundamentals(ticker: str, conn=None) -> Optional[Dict[str, float]]:
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT pe_ratio, debt_to_equity, eps_growth, operating_margin 
            FROM {settings.mimir_schema}.mimir_asset_fundamentals 
            WHERE ticker = %s
        """, (ticker,))
        row = cur.fetchone()
        if row:
            return {
                "pe_ratio": float(row[0]) if row[0] is not None else None,
                "debt_to_equity": float(row[1]) if row[1] is not None else None,
                "eps_growth": float(row[2]) if row[2] is not None else None,
                "operating_margin": float(row[3]) if row[3] is not None else None
            }
        return None
    except Exception:
        return None
    finally:
        cur.close()
        if close_conn:
            conn.close()

def get_ticker_parameters(ticker: str, conn=None) -> Optional[Dict[str, Any]]:
    """Fetches custom optimal parameters for a ticker, if tuned."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT optimal_rsi_buy, optimal_rsi_sell, optimal_sentiment, optimal_vol_ratio, optimal_hold_days, win_rate, avg_pnl
            FROM {settings.mimir_schema}.mimir_ticker_parameters
            WHERE ticker = %s
        """, (ticker,))
        row = cur.fetchone()
        if row:
            return {
                "rsi_buy": float(row[0]),
                "rsi_sell": float(row[1]),
                "sentiment": float(row[2]),
                "vol_ratio": float(row[3]),
                "hold_days": int(row[4]),
                "win_rate": float(row[5]) if row[5] is not None else None,
                "avg_pnl": float(row[6]) if row[6] is not None else None
            }
        return None
    except Exception:
        return None
    finally:
        cur.close()
        if close_conn:
            conn.close()

def get_ticker_live_feedback(ticker: str, conn=None) -> Optional[float]:
    """Calculates the success rate (%) of the last 5 evaluated signals for a ticker."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT evaluation_status 
            FROM {settings.mimir_schema}.mimir_trade_signals
            WHERE ticker = %s AND evaluation_status IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 5
        """, (ticker,))
        rows = cur.fetchall()
        if not rows:
            return None
        successes = sum(1 for row in rows if row[0] == 'SUCCESSFUL')
        return (successes / len(rows)) * 100.0
    except Exception:
        return None
    finally:
        cur.close()
        if close_conn:
            conn.close()

def scan_ticker_for_signals(ticker: str, conn=None) -> Optional[Dict[str, Any]]:
    """Scans a single ticker and returns a signal dict if triggered and inserted."""
    ticker = ticker.strip().upper()
    df = get_recent_prices(ticker, conn=conn)
    if df.empty or len(df) < 20:
        return None
        
    sentiment = get_recent_sentiment(ticker, conn=conn)
    if sentiment is None:
        # If no sentiment is recorded, fall back to neutral sentiment (0.0) or skip
        sentiment = 0.0
        
    analysis = analyze_technical_indicators(df)
    current_price = float(df['close'].iloc[-1])
    rsi = float(analysis['rsi'])
    support = float(analysis['support'])
    resistance = float(analysis['resistance'])
    trend = analysis['trend']
    
    # Load custom parameters or use global defaults
    ticker_params = get_ticker_parameters(ticker, conn=conn)
    if ticker_params:
        target_rsi_buy = ticker_params["rsi_buy"]
        target_rsi_sell = ticker_params["rsi_sell"]
        target_sentiment_buy = ticker_params["sentiment"]
        target_sentiment_sell = -ticker_params["sentiment"]
        target_vol_ratio = ticker_params["vol_ratio"]
        p_hold_days = ticker_params["hold_days"]
        p_win_rate = ticker_params["win_rate"]
        p_avg_pnl = ticker_params["avg_pnl"]
        win_str = f"{p_win_rate:.1f}%" if p_win_rate is not None else "N/A"
        pnl_str = f"{p_avg_pnl:.2f}%" if p_avg_pnl is not None else "N/A"
        param_src = f"Tuned Profile (Hold: {p_hold_days}d, Est. Win Rate: {win_str}, Est. PnL: {pnl_str})"
    else:
        target_rsi_buy = 30.0
        target_rsi_sell = 65.0
        target_sentiment_buy = 0.3
        target_sentiment_sell = -0.2
        target_vol_ratio = 1.0
        p_hold_days = 10
        param_src = "Global Backtested Default"
        
    # Check live feedback from recent evaluations to continuously learn
    feedback_note = ""
    live_success_rate = get_ticker_live_feedback(ticker, conn=conn)
    if live_success_rate is not None:
        # If live success rate of last signals is low (<= 40%), tighten thresholds
        if live_success_rate <= 40.0:
            target_rsi_buy = max(20.0, target_rsi_buy - 5.0)
            target_sentiment_buy = min(0.5, target_sentiment_buy + 0.1)
            feedback_note = f" | [LEARNING LOOP] Recent live success rate is low ({live_success_rate:.1f}%). Tightening BUY parameters (RSI <= {target_rsi_buy}, Sentiment >= {target_sentiment_buy}) to protect capital."
            
    signal_type = None
    reason = []
    
    # 1. Bullish Signals (BUY)
    if sentiment >= target_sentiment_buy:
        fundamentals = get_cached_fundamentals(ticker, conn=conn)
        passes_fundamentals = True
        fund_fail_reason = ""
        
        if fundamentals:
            pe = fundamentals.get("pe_ratio")
            de = fundamentals.get("debt_to_equity")
            eps = fundamentals.get("eps_growth")
            
            if pe is not None and (pe < 0 or pe > 35):
                passes_fundamentals = False
                fund_fail_reason = f"PE ratio ({pe:.1f}) is out of bounds (0-35)"
            if de is not None and de > 250:
                passes_fundamentals = False
                fund_fail_reason = f"Debt-to-Equity ({de:.1f}%) exceeds threshold (250%)"
            if eps is not None and eps < -0.2:
                passes_fundamentals = False
                fund_fail_reason = f"EPS growth ({eps * 100:.1f}%) is worse than -20%"
                
        if not passes_fundamentals:
            print(f"[SIGNAL_FUSION] {ticker} rejected by fundamentals overlay: {fund_fail_reason}")
        else:
            volume_ratio = float(analysis.get("volume_ratio", 1.0))
            if volume_ratio >= target_vol_ratio:
                vol_suffix = ""
                if volume_ratio >= 1.3:
                    vol_suffix = f" confirmed by anomalous volume of {volume_ratio:.2f}x average"
                    if volume_ratio >= 2.0:
                        vol_suffix += " [HIGH VOLUME BREAKOUT]"
                
                if rsi <= target_rsi_buy:
                    signal_type = 'BUY'
                    reason.append(f"Bullish sentiment ({sentiment:.2f}) aligned with oversold RSI ({rsi:.1f}){vol_suffix}. [Params: {param_src}{feedback_note}]")
                elif current_price <= support * 1.02:
                    signal_type = 'BUY'
                    reason.append(f"Bullish sentiment ({sentiment:.2f}) bouncing off support ({support:.2f}){vol_suffix}. [Params: {param_src}{feedback_note}]")
            else:
                print(f"[SIGNAL_FUSION] {ticker} rejected: volume ratio ({volume_ratio:.2f}) lacks tuned breakout expansion (<{target_vol_ratio})")
            
    # 2. Bearish Signals (SELL)
    elif sentiment <= target_sentiment_sell:
        volume_ratio = float(analysis.get("volume_ratio", 1.0))
        if volume_ratio >= target_vol_ratio:
            vol_suffix = ""
            if volume_ratio >= 1.3:
                vol_suffix = f" with volume expansion of {volume_ratio:.2f}x average"
                if volume_ratio >= 2.0:
                    vol_suffix += " [HIGH VOLUME BREAKOUT]"
                    
            if rsi >= target_rsi_sell:
                signal_type = 'SELL'
                reason.append(f"Bearish sentiment ({sentiment:.2f}) aligned with overbought RSI ({rsi:.1f}){vol_suffix}. [Params: {param_src}]")
            elif current_price >= resistance * 0.98:
                signal_type = 'SELL'
                reason.append(f"Bearish sentiment ({sentiment:.2f}) hitting resistance ({resistance:.2f}){vol_suffix}. [Params: {param_src}]")
        else:
            print(f"[SIGNAL_FUSION] {ticker} rejected: sell volume ratio ({volume_ratio:.2f}) lacks tuned expansion (<{target_vol_ratio})")
            
    if signal_type:
        reason_str = " | ".join(reason)
        # Avoid duplicate PENDING alerts
        if not check_duplicate_signal(ticker, signal_type, conn=conn):
            success = insert_trade_signal(ticker, signal_type, current_price, rsi, sentiment, support, resistance, reason_str, conn=conn)
            if success:
                return {
                    "ticker": ticker,
                    "signal_type": signal_type,
                    "trigger_price": current_price,
                    "rsi": rsi,
                    "sentiment": sentiment,
                    "support": support,
                    "resistance": resistance,
                    "reason": reason_str
                }
    return None

def scan_all_tickers() -> List[Dict[str, Any]]:
    """Runs a full scan of default tickers and dynamic tickers for signals."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Fetch default and dynamic tickers
    tickers = set([t.strip().upper() for t in DEFAULT_TICKERS if t])
    try:
        cur.execute(f"SELECT DISTINCT ticker FROM {settings.mimir_schema}.mimir_dynamic_tickers WHERE ticker IS NOT NULL")
        for row in cur.fetchall():
            tickers.add(row[0].strip().upper())
    except Exception:
        pass
    finally:
        cur.close()
        
    print(f"[SIGNAL_FUSION] Scanning {len(tickers)} tickers for trade signals...")
    new_signals = []
    try:
        for ticker in tickers:
            sig = scan_ticker_for_signals(ticker, conn=conn)
            if sig:
                new_signals.append(sig)
                print(f"[SIGNAL_FUSION] Generated {sig['signal_type']} signal for {ticker}: {sig['reason']}")
    finally:
        conn.close()
            
    return new_signals
