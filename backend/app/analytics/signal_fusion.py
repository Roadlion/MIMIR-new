# backend/app/analytics/signal_fusion.py
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional

from ..database import get_db_connection
from ..config import get_settings
from .technical_analysis import analyze_technical_indicators
from .paper_trader import is_us_stock
from ..routers.prices import DEFAULT_TICKERS
from .sentiment_momentum import (
    compute_sentiment_momentum_features,
    classify_regime,
    REGIME_ENCODING,
)
from ..services.discord_notifier import send_trade_alert as _discord_alert
from ..services.macro_tracker import is_buy_blocked_by_macro

settings = get_settings()


def build_thesis_from_reasonings(ticker: str, days: int = 7, conn=None) -> str:
    """
    Queries the top 3 highest-confidence DeepSeek reasoning strings for a ticker
    from the past N days and assembles them into a structured investment thesis.
    Zero extra LLM calls — data already in mimir_sentiment_impacts.reasoning.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    start_date = (datetime.now() - timedelta(days=days)).date()
    try:
        cur.execute(f"""
            SELECT si.reasoning, si.sentiment_score, si.confidence,
                   si.magnitude, a.title, a.published_ts
            FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
            WHERE si.ticker = %s
              AND (a.published_ts AT TIME ZONE 'UTC')::date >= %s
              AND si.reasoning IS NOT NULL
              AND si.reasoning != ''
              AND si.confidence >= 0.55
            ORDER BY si.confidence DESC, si.magnitude DESC
            LIMIT 3
        """, (ticker.strip().upper(), start_date))
        rows = cur.fetchall()
        if not rows:
            return ""

        thesis_parts = []
        for i, (reasoning, score, conf, mag, title, pub_ts) in enumerate(rows, 1):
            direction = "BULLISH" if score > 0 else "BEARISH"
            pub_date = pub_ts.strftime('%Y-%m-%d') if pub_ts else "N/A"
            thesis_parts.append(
                f"[Catalyst {i} — {mag} Magnitude | {direction} | Confidence {conf:.0%} | {pub_date}]\n"
                f"Headline: {title[:100]}\n"
                f"Analysis: {reasoning}"
            )

        return "\n\n".join(thesis_parts)
    except Exception as e:
        print(f"[SIGNAL_FUSION] build_thesis_from_reasonings error for {ticker}: {e}")
        return ""
    finally:
        cur.close()
        if close_conn:
            conn.close()


def get_magnitude_weighted_sentiment(ticker: str, days: int = 5, conn=None) -> tuple[float, float]:
    """
    Returns (weighted_sentiment, max_conviction_score) for a ticker,
    where each article's contribution is scaled by its magnitude_weight
    (HIGH=1.5, MEDIUM=1.0, LOW=0.5).
    Gives HIGH-magnitude catalysts proportionally stronger influence.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    start_date = (datetime.now() - timedelta(days=days)).date()
    try:
        cur.execute(f"""
            SELECT si.sentiment_score, si.confidence, si.magnitude
            FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
            WHERE si.ticker = %s
              AND (a.published_ts AT TIME ZONE 'UTC')::date >= %s
        """, (ticker.strip().upper(), start_date))
        rows = cur.fetchall()
        if not rows:
            return 0.0, 0.0

        mag_map = {"HIGH": 1.5, "MEDIUM": 1.0, "LOW": 0.5}
        total_weight = 0.0
        weighted_sum = 0.0
        max_conviction = 0.0
        for score, conf, mag in rows:
            w = mag_map.get(mag, 1.0)
            weighted_sum += float(score) * w
            total_weight += w
            conviction = float(score) * float(conf) * w
            if conviction > max_conviction:
                max_conviction = conviction

        weighted_sentiment = weighted_sum / total_weight if total_weight > 0 else 0.0
        return round(weighted_sentiment, 4), round(max_conviction, 4)
    except Exception as e:
        print(f"[SIGNAL_FUSION] get_magnitude_weighted_sentiment error for {ticker}: {e}")
        return 0.0, 0.0
    finally:
        cur.close()
        if close_conn:
            conn.close()


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
    """Checks if there's already a PENDING or recent signal of the same type for this ticker (within 12 hours)."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    
    sql = f"""
        SELECT 1 FROM {settings.mimir_schema}.mimir_trade_signals
        WHERE ticker = %s AND signal_type = %s
          AND (status = 'PENDING' OR created_at >= NOW() - INTERVAL '12 hours')
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

def insert_trade_signal(
    ticker: str,
    signal_type: str,
    price: float,
    rsi: float,
    sentiment: float,
    support: float,
    resistance: float,
    reason: str,
    catalyst_type: Optional[str] = "SENTIMENT_FUSION",
    holding_period: Optional[str] = "Swing Horizon (3-7 days)",
    investment_thesis: Optional[str] = None,
    headline: Optional[str] = None,
    target_price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    conviction_score: Optional[float] = None,
    conn=None
) -> bool:
    """
    [PERMANENTLY DEPRECATED FOR STANDALONE EMISSION]
    Only the War Rig Transmission Engine (catalyst_type == 'WAR_RIG_CONVERGENCE')
    is authorized to emit trade signals into mimir_trade_signals.
    All standalone sentiment fusion, surge, or unvetted emissions are blocked.
    """
    if catalyst_type != "WAR_RIG_CONVERGENCE":
        print(f"[SIGNAL_FUSION DECOMMISSIONED] Standalone signal for {ticker} ({catalyst_type}) blocked. Only War Rig Transmission Engine is authorized.")
        return False

    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    
    if target_price is None:
        target_price = resistance if resistance and resistance > price else price * 1.06
    if stop_loss is None:
        stop_loss = support if support and support < price else price * 0.95

    sql = f"""
        INSERT INTO {settings.mimir_schema}.mimir_trade_signals 
        (ticker, signal_type, trigger_price, rsi_value, sentiment_score, support_level, resistance_level, reason, status,
         catalyst_type, holding_period, investment_thesis, headline, target_price, stop_loss, conviction_score)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'PENDING', %s, %s, %s, %s, %s, %s, %s)
    """
    try:
        cur.execute(sql, (
            ticker, signal_type, price, rsi, sentiment, support, resistance, reason,
            catalyst_type, holding_period, investment_thesis, headline, target_price, stop_loss, conviction_score
        ))
        conn.commit()

        # ── Discord notification ─────────────────────────────────────────────
        try:
            _discord_alert(
                ticker=ticker,
                signal_type=signal_type,
                trigger_price=price,
                target_price=target_price,
                stop_loss=stop_loss,
                sentiment_score=sentiment,
                catalyst_type=catalyst_type or "SENTIMENT_FUSION",
                headline=headline,
                reason=reason,
                holding_period=holding_period,
                conviction_score=conviction_score,
                rsi=rsi,
            )
        except Exception as discord_err:
            print(f"[SIGNAL_FUSION] Discord notify failed (non-fatal): {discord_err}")
        # ────────────────────────────────────────────────────────────────────

        return True
    except Exception as e:
        print(f"[SIGNAL_FUSION] Error inserting signal: {e}")
        return False
    finally:
        cur.close()
        if close_conn:
            conn.close()

def get_cached_fundamentals(ticker: str, conn=None) -> Optional[Dict[str, Any]]:
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT pe_ratio, debt_to_equity, eps_growth, operating_margin,
                   ev_ebitda, fcf_yield, dcf_intrinsic_value, valuation_status
            FROM {settings.mimir_schema}.mimir_asset_fundamentals 
            WHERE ticker = %s
        """, (ticker,))
        row = cur.fetchone()
        if row:
            return {
                "pe_ratio": float(row[0]) if row[0] is not None else None,
                "debt_to_equity": float(row[1]) if row[1] is not None else None,
                "eps_growth": float(row[2]) if row[2] is not None else None,
                "operating_margin": float(row[3]) if row[3] is not None else None,
                "ev_ebitda": float(row[4]) if row[4] is not None else None,
                "fcf_yield": float(row[5]) if row[5] is not None else None,
                "dcf_intrinsic_value": float(row[6]) if row[6] is not None else None,
                "valuation_status": str(row[7]) if row[7] is not None else "FAIRLY_VALUED"
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
            SELECT optimal_rsi_buy, optimal_rsi_sell, optimal_sentiment, optimal_vol_ratio, optimal_hold_days, 
                   win_rate, avg_pnl, optimal_prob_buy, optimal_prob_sell, selected_features_buy, selected_features_sell
            FROM {settings.mimir_schema}.mimir_ticker_parameters
            WHERE ticker = %s
        """, (ticker,))
        row = cur.fetchone()
        if row:
            import json
            return {
                "rsi_buy": float(row[0]),
                "rsi_sell": float(row[1]),
                "sentiment": float(row[2]),
                "vol_ratio": float(row[3]),
                "hold_days": int(row[4]),
                "win_rate": float(row[5]) if row[5] is not None else None,
                "avg_pnl": float(row[6]) if row[6] is not None else None,
                "prob_buy": float(row[7]) if row[7] is not None else 0.55,
                "prob_sell": float(row[8]) if row[8] is not None else 0.55,
                "selected_features_buy": json.loads(row[9]) if row[9] else None,
                "selected_features_sell": json.loads(row[10]) if row[10] else None
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

def get_daily_sentiment_history(ticker: str, days: int = 20, conn=None) -> pd.DataFrame:
    """Fetches a DataFrame of daily average sentiment scores for a single ticker,
    timezone-aligned to prevent lookahead leakage.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    
    start_date = (datetime.now() - timedelta(days=days)).date()
    
    sql = f"""
        WITH adjusted_sentiment AS (
            SELECT 
                   CASE 
                       -- Crypto (closes at 00:00 UTC)
                       WHEN si.ticker LIKE '%%-USD' THEN 
                           (a.published_ts AT TIME ZONE 'UTC')::date
                       
                       -- Forex / Commodity (settles at 17:00 NY)
                       WHEN si.ticker LIKE '%%=X' OR si.ticker LIKE '%%=F' THEN
                           CASE 
                               WHEN EXTRACT(HOUR FROM (a.published_ts AT TIME ZONE 'America/New_York')) * 60 + EXTRACT(MINUTE FROM (a.published_ts AT TIME ZONE 'America/New_York')) >= 1020 THEN
                                   ((a.published_ts AT TIME ZONE 'America/New_York') + INTERVAL '1 day')::date
                               ELSE
                                   (a.published_ts AT TIME ZONE 'America/New_York')::date
                           END

                       -- China (closes at 15:00 Shanghai)
                       WHEN si.ticker LIKE '%%.SS' OR si.ticker LIKE '%%.SZ' THEN
                           CASE 
                               WHEN EXTRACT(HOUR FROM (a.published_ts AT TIME ZONE 'Asia/Shanghai')) * 60 + EXTRACT(MINUTE FROM (a.published_ts AT TIME ZONE 'Asia/Shanghai')) >= 900 THEN
                                   ((a.published_ts AT TIME ZONE 'Asia/Shanghai') + INTERVAL '1 day')::date
                               ELSE
                                   (a.published_ts AT TIME ZONE 'Asia/Shanghai')::date
                           END

                       -- Korea (closes at 15:30 Seoul)
                       WHEN si.ticker LIKE '%%.KS' THEN
                           CASE 
                               WHEN EXTRACT(HOUR FROM (a.published_ts AT TIME ZONE 'Asia/Seoul')) * 60 + EXTRACT(MINUTE FROM (a.published_ts AT TIME ZONE 'Asia/Seoul')) >= 930 THEN
                                   ((a.published_ts AT TIME ZONE 'Asia/Seoul') + INTERVAL '1 day')::date
                               ELSE
                                   (a.published_ts AT TIME ZONE 'Asia/Seoul')::date
                           END

                       -- Japan (closes at 15:00 Tokyo)
                       WHEN si.ticker LIKE '%%.T' THEN
                           CASE 
                               WHEN EXTRACT(HOUR FROM (a.published_ts AT TIME ZONE 'Asia/Tokyo')) * 60 + EXTRACT(MINUTE FROM (a.published_ts AT TIME ZONE 'Asia/Tokyo')) >= 900 THEN
                                   ((a.published_ts AT TIME ZONE 'Asia/Tokyo') + INTERVAL '1 day')::date
                               ELSE
                                   (a.published_ts AT TIME ZONE 'Asia/Tokyo')::date
                           END

                       -- US (closes at 16:00 NY)
                       ELSE
                           CASE 
                               WHEN EXTRACT(HOUR FROM (a.published_ts AT TIME ZONE 'America/New_York')) * 60 + EXTRACT(MINUTE FROM (a.published_ts AT TIME ZONE 'America/New_York')) >= 960 THEN
                                   ((a.published_ts AT TIME ZONE 'America/New_York') + INTERVAL '1 day')::date
                               ELSE
                                   (a.published_ts AT TIME ZONE 'America/New_York')::date
                           END
                   END as date,
                   si.sentiment_score,
                   si.article_id
            FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
            WHERE si.ticker = %s AND (a.published_ts AT TIME ZONE 'UTC')::date >= %s
        )
        SELECT date, AVG(sentiment_score) as sentiment, COUNT(article_id) as sent_vol
        FROM adjusted_sentiment
        GROUP BY date
        ORDER BY date ASC
    """
    try:
        cur.execute(sql, (ticker, start_date))
        rows = cur.fetchall()
        if not rows:
            return pd.DataFrame(columns=['date', 'sentiment', 'sent_vol'])
        df_sent = pd.DataFrame(rows, columns=['date', 'sentiment', 'sent_vol'])
        df_sent['date'] = pd.to_datetime(df_sent['date']).dt.date
        df_sent['sentiment'] = pd.to_numeric(df_sent['sentiment'], errors='coerce').astype(float)
        df_sent['sent_vol'] = pd.to_numeric(df_sent['sent_vol'], errors='coerce').astype(float)
        return df_sent
    except Exception as e:
        print(f"[SIGNAL_FUSION] Error fetching sentiment history for {ticker}: {e}")
        return pd.DataFrame(columns=['date', 'sentiment', 'sent_vol'])
    finally:
        cur.close()
        if close_conn:
            conn.close()

def get_xgb_prediction(*args, **kwargs) -> float:
    """DEPRECATED: Legacy XGBoost inference replaced by Unified War Rig Crankshaft Transmission."""
    return 0.50


def scan_ticker_for_signals(*args, **kwargs) -> Optional[Dict[str, Any]]:
    """DEPRECATED: Legacy ML scanner decommissioned. Replaced by Unified War Rig Transmission Engine."""
    return None


def scan_all_tickers() -> List[Dict[str, Any]]:
    """Runs a full scan of candidate tickers for signals using the War Rig Crankshaft Transmission."""
    conn = get_db_connection()
    new_signals = []
    try:
        from .war_rig_engine import run_war_rig_scan
        war_rig_sigs = run_war_rig_scan(conn=conn, top_n=10)
        if war_rig_sigs:
            new_signals.extend(war_rig_sigs)
            print(f"[SIGNAL_FUSION] War Rig Crankshaft generated {len(war_rig_sigs)} WAR_RIG_CONVERGENCE signals.")
    except Exception as wr_err:
        print(f"[SIGNAL_FUSION] War Rig scan error: {wr_err}")
    finally:
        conn.close()
            
    return new_signals

def validate_sentiment_with_price(price_cache):
    """Event-driven hook to invalidate LLM sentiment signals if 1-minute price action strongly diverges."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        
        # Find highly polarized sentiment impacts from the last 30 minutes
        cur.execute(f"""
            SELECT article_id, asset_name, ticker, sentiment_score 
            FROM {settings.mimir_schema}.mimir_sentiment_impacts 
            WHERE created_at >= NOW() - INTERVAL '30 minutes'
            AND abs(sentiment_score) > 0.5
        """)
        recent_impacts = cur.fetchall()
        
        for impact in recent_impacts:
            article_id, asset_name, ticker, score = impact
            
            if ticker not in price_cache or len(price_cache[ticker]) < 15:
                continue
                
            ticks = list(price_cache[ticker])
            start_price = ticks[-15]['close']
            end_price = ticks[-1]['close']
            pct_change = (end_price - start_price) / start_price
            
            # If Sentiment is very bullish (> 0.5) but price drops > 1% in 15m (Bull Trap)
            if score > 0.5 and pct_change <= -0.01:
                cur.execute(f"""
                    UPDATE {settings.mimir_schema}.mimir_sentiment_impacts
                    SET sentiment_score = 0.0
                    WHERE article_id = %s AND asset_name = %s
                """, (article_id, asset_name))
                print(f"[SENTIMENT FILTER] Neutralized BULLISH sentiment for {ticker} (Price dropped {pct_change*100:.2f}%)")
            
            # If Sentiment is very bearish (< -0.5) but price pumps > 1% in 15m (Bear Trap)
            elif score < -0.5 and pct_change >= 0.01:
                cur.execute(f"""
                    UPDATE {settings.mimir_schema}.mimir_sentiment_impacts
                    SET sentiment_score = 0.0
                    WHERE article_id = %s AND asset_name = %s
                """, (article_id, asset_name))
                print(f"[SENTIMENT FILTER] Neutralized BEARISH sentiment for {ticker} (Price surged {pct_change*100:.2f}%)")
                
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[SENTIMENT FILTER ERROR] {e}")
    finally:
        cur.close()
        conn.close()
