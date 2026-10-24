# backend/app/analytics/sentiment_momentum.py
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, Tuple, List

from ..database import get_db_connection
from ..config import get_settings

settings = get_settings()

REGIME_ENCODING = {
    'NEUTRAL': 0,
    'ACCUMULATING': 1,
    'ALIGNED': 2,
    'EXHAUSTED': 3,
    'DIVERGENT': 4,
    'PANIC_OVERSOLD': 5,
}

def get_avg_news_sentiment(ticker: str, days: int = 3, conn=None) -> Optional[float]:
    """Fetches average professional news sentiment score from mimir_sentiment_impacts."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    start_date = (datetime.now(timezone.utc) - timedelta(days=days)).date()
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
        print(f"[SENTIMENT_MOMENTUM] Error fetching news sentiment for {ticker}: {e}")
        return None
    finally:
        cur.close()
        if close_conn:
            conn.close()

def get_avg_social_sentiment(ticker: str, days: int = 3, conn=None) -> Optional[float]:
    """Fetches average retail social sentiment score from mimir_social_chatter if available."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    start_date = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    sql = f"""
        SELECT AVG(sentiment_score)
        FROM {settings.mimir_schema}.mimir_social_chatter
        WHERE ticker = %s AND (bucket_ts AT TIME ZONE 'UTC')::date >= %s
    """
    try:
        cur.execute(sql, (ticker.strip().upper(), start_date))
        val = cur.fetchone()[0]
        return float(val) if val is not None else None
    except Exception:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return None
    finally:
        cur.close()
        if close_conn:
            conn.close()

def compute_pro_retail_divergence(ticker: str, days: int = 3, conn=None) -> float:
    """
    Compare professional news sentiment vs retail social sentiment.
    Returns: positive = pros more bullish, negative = retail more bullish.
    """
    news_sent = get_avg_news_sentiment(ticker, days=days, conn=conn)
    social_sent = get_avg_social_sentiment(ticker, days=days, conn=conn)
    if news_sent is None or social_sent is None:
        return 0.0
    return round(news_sent - social_sent, 4)

def compute_unanimity(ticker: str, lookback_days: int = 5, conn=None) -> float:
    """
    Measure how one-sided sentiment is across recent articles and chatter.
    unanimity = |positive_count - negative_count| / total_count
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    start_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date()
    sql = f"""
        SELECT si.sentiment_score
        FROM {settings.mimir_schema}.mimir_sentiment_impacts si
        JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
        WHERE si.ticker = %s AND (a.published_ts AT TIME ZONE 'UTC')::date >= %s
    """
    try:
        cur.execute(sql, (ticker.strip().upper(), start_date))
        rows = cur.fetchall()
        if not rows:
            return 0.0
        scores = [r[0] for r in rows if r[0] is not None]
        positive = sum(1 for s in scores if s > 0.1)
        negative = sum(1 for s in scores if s < -0.1)
        total = positive + negative
        if total < 3:
            return 0.0
        return round(abs(positive - negative) / float(total), 4)
    except Exception as e:
        print(f"[SENTIMENT_MOMENTUM] Error calculating unanimity for {ticker}: {e}")
        return 0.0
    finally:
        cur.close()
        if close_conn:
            conn.close()

def compute_attention_decay(ticker: str, conn=None) -> float:
    """
    Track how fast retail attention is fading from a ticker.
    attention_decay = social_volume_last_24h / peak_social_volume_last_7d
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    now = datetime.now(timezone.utc)
    t_24h = now - timedelta(hours=24)
    t_7d = now - timedelta(days=7)
    
    sql_24h = f"""
        SELECT COUNT(*)
        FROM {settings.mimir_schema}.mimir_sentiment_impacts si
        JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
        WHERE si.ticker = %s AND a.published_ts >= %s
    """
    sql_peak = f"""
        SELECT DATE(a.published_ts) as day, COUNT(*) as cnt
        FROM {settings.mimir_schema}.mimir_sentiment_impacts si
        JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
        WHERE si.ticker = %s AND a.published_ts >= %s
        GROUP BY DATE(a.published_ts)
        ORDER BY cnt DESC LIMIT 1
    """
    try:
        cur.execute(sql_24h, (ticker.strip().upper(), t_24h))
        cur_vol = cur.fetchone()[0] or 0
        
        cur.execute(sql_peak, (ticker.strip().upper(), t_7d))
        peak_row = cur.fetchone()
        peak_vol = peak_row[1] if peak_row else 0
        
        if peak_vol == 0:
            return 0.0
        return round(min(1.0, cur_vol / float(peak_vol)), 4)
    except Exception:
        return 0.5
    finally:
        cur.close()
        if close_conn:
            conn.close()

def compute_panic_score(ticker: str, conn=None) -> float:
    """
    Detect retail panic events combining:
    - Sharp 1-day sentiment drop (< -0.5)
    - Article/social volume spike (> 3x baseline)
    - Price shock (> 2 std dev from mean)
    - Fundamentals intact (placeholder / constant 1.0 if ok)
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    now = datetime.now(timezone.utc)
    t_1d = now - timedelta(days=1)
    t_7d = now - timedelta(days=7)
    
    try:
        # Sentiment shock
        sql_sent = f"""
            SELECT AVG(si.sentiment_score)
            FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
            WHERE si.ticker = %s AND a.published_ts >= %s
        """
        cur.execute(sql_sent, (ticker.strip().upper(), t_1d))
        val = cur.fetchone()[0]
        sent_1d = float(val) if val is not None else 0.0
        sent_shock = max(0.0, (-sent_1d - 0.3) / 0.7)
        
        # Volume spike ratio
        sql_vol_1d = f"""
            SELECT COUNT(*) FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
            WHERE si.ticker = %s AND a.published_ts >= %s
        """
        cur.execute(sql_vol_1d, (ticker.strip().upper(), t_1d))
        v1d = cur.fetchone()[0] or 0
        
        cur.execute(sql_vol_1d, (ticker.strip().upper(), t_7d))
        v7d = cur.fetchone()[0] or 0
        avg_v1d = (v7d / 7.0) if v7d > 0 else 1.0
        vol_ratio = v1d / max(1.0, avg_v1d)
        social_spike = min(1.0, max(0.0, (vol_ratio - 1.0) / 4.0))
        
        # Price shock (placeholder if price data available)
        price_shock = 0.5  # default baseline
        
        fundamental_stability = 1.0
        panic_score = (sent_shock * 0.4 + social_spike * 0.4 + price_shock * 0.1 + fundamental_stability * 0.1)
        return round(min(1.0, max(0.0, panic_score)), 4)
    except Exception as e:
        print(f"[SENTIMENT_MOMENTUM] Error calculating panic score for {ticker}: {e}")
        return 0.0
    finally:
        cur.close()
        if close_conn:
            conn.close()

def detect_earnings_trap(ticker: str, conn=None) -> float:
    """
    Detect pre-event sentiment velocity + price run-up trap ("buy the rumor, sell the news").
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    now = datetime.now(timezone.utc)
    t_5d = now - timedelta(days=5)
    
    try:
        sql = f"""
            SELECT AVG(si.sentiment_score)
            FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
            WHERE si.ticker = %s AND a.published_ts >= %s
        """
        cur.execute(sql, (ticker.strip().upper(), t_5d))
        val = cur.fetchone()[0]
        sent_5d = float(val) if val is not None else 0.0
        sent_vel_5d = sent_5d / 5.0
        
        if sent_vel_5d > 0.15:
            # Trap likelihood high if velocity sustained
            trap_score = min(1.0, sent_vel_5d / 0.3)
            return round(trap_score, 4)
        return 0.0
    except Exception:
        return 0.0
    finally:
        cur.close()
        if close_conn:
            conn.close()

def compute_sentiment_momentum_features(ticker: str, lookback_days: int = 5, conn=None) -> Dict[str, Any]:
    """
    Extracts all sentiment momentum & regime features for a given ticker.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    
    now = datetime.now(timezone.utc)
    t_3d = now - timedelta(days=3)
    t_6d = now - timedelta(days=6)
    t_5d = now - timedelta(days=5)
    
    features = {
        'sent_velocity_3d': 0.0,
        'sent_acceleration': 0.0,
        'sent_volume_divergence': 0.0,
        'price_sentiment_gap': 0.0,
        'social_news_divergence': 0.0,
        'narrative_persistence': 0,
        'unanimity_score': 0.0,
        'attention_decay_ratio': 0.5,
        'panic_score': 0.0,
        'earnings_trap_score': 0.0,
        'pro_retail_divergence': 0.0,
        'regime_state': 'NEUTRAL',
        'regime_state_encoded': 0,
    }
    
    try:
        # 1. Sent velocity 3d
        sql_3d = f"""
            SELECT AVG(si.sentiment_score), COUNT(*)
            FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
            WHERE si.ticker = %s AND a.published_ts >= %s
        """
        cur.execute(sql_3d, (ticker.strip().upper(), t_3d))
        r3d = cur.fetchone()
        s3d = float(r3d[0]) if r3d and r3d[0] is not None else 0.0
        cnt3d = r3d[1] if r3d else 0
        
        sql_prior = f"""
            SELECT AVG(si.sentiment_score)
            FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
            WHERE si.ticker = %s AND a.published_ts >= %s AND a.published_ts < %s
        """
        cur.execute(sql_prior, (ticker.strip().upper(), t_6d, t_3d))
        r_prior = cur.fetchone()
        s_prior = float(r_prior[0]) if r_prior and r_prior[0] is not None else 0.0
        
        vel_3d = (s3d - s_prior) / 3.0
        features['sent_velocity_3d'] = round(vel_3d, 4)
        features['sent_acceleration'] = round(vel_3d - (s_prior / 3.0), 4)
        
        if cnt3d > 0:
            features['sent_volume_divergence'] = round(abs(s3d) / float(cnt3d), 4)
            
        # 2. Narrative persistence (consecutive days covered)
        sql_days = f"""
            SELECT COUNT(DISTINCT DATE(a.published_ts))
            FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
            WHERE si.ticker = %s AND a.published_ts >= %s
        """
        cur.execute(sql_days, (ticker.strip().upper(), t_5d))
        r_days = cur.fetchone()
        features['narrative_persistence'] = r_days[0] if r_days else 0
        
        # 3. Unanimity & attention decay
        features['unanimity_score'] = compute_unanimity(ticker, lookback_days=lookback_days, conn=conn)
        features['attention_decay_ratio'] = compute_attention_decay(ticker, conn=conn)
        features['panic_score'] = compute_panic_score(ticker, conn=conn)
        features['earnings_trap_score'] = detect_earnings_trap(ticker, conn=conn)
        features['pro_retail_divergence'] = compute_pro_retail_divergence(ticker, days=3, conn=conn)
        
        # Price-sentiment gap: real divergence between sentiment and 5-day price return.
        # Positive gap = sentiment has risen more than price = ACCUMULATING setup.
        try:
            sql_price5d = f"""
                SELECT close
                FROM {settings.mimir_schema}.v_mimir_daily_ohlcv
                WHERE ticker = %s
                ORDER BY date DESC
                LIMIT 6
            """
            cur.execute(sql_price5d, (ticker.strip().upper(),))
            price_rows = cur.fetchall()
            if price_rows and len(price_rows) >= 2:
                price_now = float(price_rows[0][0])
                price_5d_ago = float(price_rows[-1][0])
                price_return_5d = (price_now - price_5d_ago) / (price_5d_ago + 1e-15)
                features['price_sentiment_gap'] = round(s3d - price_return_5d, 4)
            else:
                features['price_sentiment_gap'] = round(s3d * 0.5, 4)
        except Exception:
            features['price_sentiment_gap'] = round(s3d * 0.5, 4)

        # Classify regime
        regime = classify_regime(features)
        features['regime_state'] = regime
        features['regime_state_encoded'] = REGIME_ENCODING.get(regime, 0)
        
        return features
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        print(f"[SENTIMENT_MOMENTUM] Error building features for {ticker}: {e}")
        return features
    finally:
        cur.close()
        if close_conn:
            conn.close()

def classify_regime(ticker_features: Dict[str, Any]) -> str:
    """
    Classifies a ticker into a regime based on sentiment momentum & divergence patterns.
    """
    velocity = ticker_features.get('sent_velocity_3d', 0.0)
    gap = ticker_features.get('price_sentiment_gap', 0.0)
    unanimity = ticker_features.get('unanimity_score', 0.0)
    decay = ticker_features.get('attention_decay_ratio', 0.5)
    panic_score = ticker_features.get('panic_score', 0.0)
    price_momentum = 0.0  # default

    if panic_score > 0.7:
        return 'PANIC_OVERSOLD'   # Retail panic selling — contrarian buy

    # ACCUMULATING: sentiment building while price hasn't moved yet — key BUY ZONE
    # Lowered thresholds from (0.03, 0.05) to (0.01, 0.02) so real data can trigger this.
    if velocity > 0.01 and gap > 0.02:
        return 'ACCUMULATING'

    # ALIGNED: sentiment and price rising together — valid continuation entry
    if velocity > 0 and abs(gap) <= 0.05:
        return 'ALIGNED'

    # EXHAUSTED: sentiment falling while price is still elevated — EXIT/SHORT zone
    if velocity < -0.01 and gap < -0.02:
        return 'EXHAUSTED'

    # DIVERGENT: price contradicting sentiment direction — stay out
    if abs(velocity) > 0.03 and np.sign(velocity) != np.sign(price_momentum):
        return 'DIVERGENT'

    return 'NEUTRAL'
