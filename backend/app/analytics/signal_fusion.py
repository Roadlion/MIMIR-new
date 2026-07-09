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

def get_recent_prices(ticker: str, days: int = 120) -> pd.DataFrame:
    """Fetches recent daily prices for a ticker from the database."""
    conn = get_db_connection()
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
        conn.close()

def get_recent_sentiment(ticker: str, days: int = 5) -> Optional[float]:
    """Fetches the average sentiment score for a ticker over the last N days."""
    conn = get_db_connection()
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
        conn.close()

def check_duplicate_signal(ticker: str, signal_type: str) -> bool:
    """Checks if there's already a PENDING signal of the same type for this ticker."""
    conn = get_db_connection()
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
        conn.close()

def insert_trade_signal(ticker: str, signal_type: str, price: float, rsi: float, sentiment: float, support: float, resistance: float, reason: str) -> bool:
    """Inserts a new trade signal into the database."""
    conn = get_db_connection()
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
        conn.close()

def scan_ticker_for_signals(ticker: str) -> Optional[Dict[str, Any]]:
    """Scans a single ticker and returns a signal dict if triggered and inserted."""
    ticker = ticker.strip().upper()
    df = get_recent_prices(ticker)
    if df.empty or len(df) < 20:
        return None
        
    sentiment = get_recent_sentiment(ticker)
    if sentiment is None:
        # If no sentiment is recorded, fall back to neutral sentiment (0.0) or skip
        sentiment = 0.0
        
    analysis = analyze_technical_indicators(df)
    current_price = float(df['close'].iloc[-1])
    rsi = float(analysis['rsi'])
    support = float(analysis['support'])
    resistance = float(analysis['resistance'])
    trend = analysis['trend']
    
    signal_type = None
    reason = []
    
    # 1. Bullish Signals (BUY)
    if sentiment >= 0.2:
        if rsi <= 40:
            signal_type = 'BUY'
            reason.append(f"Bullish sentiment ({sentiment:.2f}) aligned with oversold RSI ({rsi:.1f}).")
        elif current_price <= support * 1.02:
            signal_type = 'BUY'
            reason.append(f"Bullish sentiment ({sentiment:.2f}) with price ({current_price:.2f}) bouncing off support ({support:.2f}).")
            
    # 2. Bearish Signals (SELL)
    elif sentiment <= -0.2:
        if rsi >= 65:
            signal_type = 'SELL'
            reason.append(f"Bearish sentiment ({sentiment:.2f}) aligned with overbought RSI ({rsi:.1f}).")
        elif current_price >= resistance * 0.98:
            signal_type = 'SELL'
            reason.append(f"Bearish sentiment ({sentiment:.2f}) with price ({current_price:.2f}) hitting resistance ({resistance:.2f}).")
            
    if signal_type:
        reason_str = " | ".join(reason)
        # Avoid duplicate PENDING alerts
        if not check_duplicate_signal(ticker, signal_type):
            success = insert_trade_signal(ticker, signal_type, current_price, rsi, sentiment, support, resistance, reason_str)
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
        conn.close()
        
    print(f"[SIGNAL_FUSION] Scanning {len(tickers)} tickers for trade signals...")
    new_signals = []
    for ticker in tickers:
        sig = scan_ticker_for_signals(ticker)
        if sig:
            new_signals.append(sig)
            print(f"[SIGNAL_FUSION] Generated {sig['signal_type']} signal for {ticker}: {sig['reason']}")
            
    return new_signals
