# backend/app/services/macro_tracker.py
"""
MIMIR Macro Signal Tracker
===========================
Persists Central Bank policy signals extracted by DeepSeek into
`mimir_macro_signals` so the rest of the pipeline can query the
live macro regime without extra LLM calls.

Schema (auto-created on first use):
    CREATE TABLE IF NOT EXISTS yggdrasil.mimir_macro_signals (
        id              SERIAL PRIMARY KEY,
        institution     TEXT NOT NULL,          -- 'Federal Reserve', 'ECB', etc.
        policy_signal   TEXT NOT NULL,          -- hawkish | dovish | bullish | bearish | neutral
        sentiment_score FLOAT,
        headline        TEXT,
        article_id      INT,
        recorded_at     TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE(institution, article_id)
    );

Rate-sensitive sectors blocked on Fed hawkish regime:
    REAL_ESTATE, UTILITIES, FINANCIAL_SERVICES
"""

import logging
from typing import Optional, List
from ..database import get_db_connection
from ..config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Central banks DeepSeek tags with policy_signal
CENTRAL_BANKS = {
    "Federal Reserve", "Fed", "FOMC",
    "ECB", "European Central Bank",
    "BOJ", "Bank of Japan",
    "PBOC", "People's Bank of China",
    "BOE", "Bank of England",
}

# Canonical names for deduplication
_CANONICAL_BANK = {
    "fed": "Federal Reserve",
    "fomc": "Federal Reserve",
    "federal reserve": "Federal Reserve",
    "ecb": "ECB",
    "european central bank": "ECB",
    "boj": "BOJ",
    "bank of japan": "BOJ",
    "pboc": "PBOC",
    "people's bank of china": "PBOC",
    "boe": "BOE",
    "bank of england": "BOE",
}

# Rate-sensitive equity sectors blocked when Fed is hawkish
RATE_SENSITIVE_SECTORS = {"REAL_ESTATE", "UTILITIES", "FINANCIAL_SERVICES"}


def _ensure_table(conn):
    """Create mimir_macro_signals if it doesn't exist."""
    try:
        cur = conn.cursor()
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {settings.mimir_schema}.mimir_macro_signals (
                id              SERIAL PRIMARY KEY,
                institution     TEXT NOT NULL,
                policy_signal   TEXT NOT NULL,
                sentiment_score FLOAT,
                headline        TEXT,
                article_id      INT,
                recorded_at     TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(institution, article_id)
            )
        """)
        conn.commit()
        cur.close()
    except Exception as e:
        conn.rollback()
        logger.warning(f"[MACRO_TRACKER] Could not ensure table: {e}")


def upsert_policy_signal(
    institution: str,
    policy_signal: str,
    sentiment_score: float,
    headline: str,
    article_id: int,
    conn=None
) -> bool:
    """
    Upserts a central bank policy signal observation.
    Called from sentiment_processor after each article is scored.
    """
    canonical = _CANONICAL_BANK.get(institution.strip().lower(), institution.strip())

    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    _ensure_table(conn)
    cur = conn.cursor()
    try:
        cur.execute(f"""
            INSERT INTO {settings.mimir_schema}.mimir_macro_signals
                (institution, policy_signal, sentiment_score, headline, article_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (institution, article_id) DO UPDATE
                SET policy_signal   = EXCLUDED.policy_signal,
                    sentiment_score = EXCLUDED.sentiment_score,
                    headline        = EXCLUDED.headline,
                    recorded_at     = NOW()
        """, (canonical, policy_signal, sentiment_score, headline[:500] if headline else None, article_id))
        conn.commit()
        logger.info(f"[MACRO_TRACKER] {canonical} → {policy_signal} (article {article_id})")
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"[MACRO_TRACKER] Upsert failed: {e}")
        return False
    finally:
        cur.close()
        if close_conn:
            conn.close()


def get_latest_fed_signal(conn=None) -> Optional[str]:
    """
    Returns the most recent Federal Reserve policy_signal string,
    or None if no data exists.
    e.g. 'hawkish', 'dovish', 'neutral'
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    _ensure_table(conn)
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT policy_signal
            FROM {settings.mimir_schema}.mimir_macro_signals
            WHERE institution = 'Federal Reserve'
            ORDER BY recorded_at DESC
            LIMIT 1
        """)
        row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.error(f"[MACRO_TRACKER] get_latest_fed_signal error: {e}")
        return None
    finally:
        cur.close()
        if close_conn:
            conn.close()


def get_macro_regime_summary(conn=None) -> dict:
    """
    Returns a dict of {institution: latest_policy_signal} for all tracked CBs.
    Used by the Oracle and the UI macro header.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    _ensure_table(conn)
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT DISTINCT ON (institution)
                institution, policy_signal, sentiment_score, headline, recorded_at
            FROM {settings.mimir_schema}.mimir_macro_signals
            ORDER BY institution, recorded_at DESC
        """)
        rows = cur.fetchall()
        return {
            row[0]: {
                "policy_signal": row[1],
                "sentiment_score": float(row[2]) if row[2] else None,
                "headline": row[3],
                "recorded_at": row[4].isoformat() if row[4] else None,
            }
            for row in rows
        }
    except Exception as e:
        logger.error(f"[MACRO_TRACKER] get_macro_regime_summary error: {e}")
        return {}
    finally:
        cur.close()
        if close_conn:
            conn.close()


def is_buy_blocked_by_macro(ticker_sector: Optional[str], conn=None) -> tuple[bool, str]:
    """
    Returns (blocked: bool, reason: str).
    Blocks BUY signals for rate-sensitive sectors when Fed is hawkish.
    Completely free — reads from mimir_macro_signals, no LLM call.
    """
    if not ticker_sector or ticker_sector not in RATE_SENSITIVE_SECTORS:
        return False, ""

    fed_signal = get_latest_fed_signal(conn=conn)
    if fed_signal and fed_signal.lower() in ("hawkish", "bearish"):
        reason = (
            f"Fed macro regime is '{fed_signal}' — BUY blocked for "
            f"rate-sensitive sector ({ticker_sector}). "
            f"Rising rate environment compresses valuations in this sector."
        )
        return True, reason

    return False, ""


def is_market_regime_bullish(conn=None) -> tuple[bool, str]:
    """
    Checks if the broader US equity market is in a healthy, bullish/neutral regime:
    1. SPY is trading above its 50-day Simple Moving Average (SMA_50).
    2. ^VIX is at or below 25.0 (absence of high-volatility panic).
    Returns (is_bullish: bool, reason: str).
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    cur = conn.cursor()
    try:
        # 1. Fetch SPY prices for 50-day SMA
        cur.execute(f"""
            SELECT close
            FROM {settings.mimir_schema}.v_mimir_daily_ohlcv
            WHERE ticker = 'SPY'
            ORDER BY date DESC
            LIMIT 50
        """)
        spy_rows = cur.fetchall()

        if len(spy_rows) >= 20:
            spy_closes = [float(r['close'] if isinstance(r, dict) else r[0]) for r in spy_rows]
            latest_spy = spy_closes[0]
            sma_50 = sum(spy_closes) / len(spy_closes)

            if latest_spy < sma_50:
                return False, f"Market regime defensive: SPY (${latest_spy:.2f}) < 50-day SMA (${sma_50:.2f}). Swing longs paused."

        # 2. Fetch latest ^VIX level
        cur.execute(f"""
            SELECT close
            FROM {settings.mimir_schema}.v_mimir_daily_ohlcv
            WHERE ticker IN ('^VIX', 'VIX')
            ORDER BY date DESC
            LIMIT 1
        """)
        vix_row = cur.fetchone()
        if vix_row is not None:
            raw_vix = vix_row['close'] if isinstance(vix_row, dict) else vix_row[0]
            if raw_vix is not None:
                latest_vix = float(raw_vix)
                if latest_vix > 25.0:
                    return False, f"Market regime elevated risk: VIX at {latest_vix:.1f} > 25.0 threshold. Swing longs paused."

        return True, "Market regime healthy (SPY >= 50-SMA and VIX <= 25.0)."
    except Exception as e:
        logger.warning(f"[MACRO_TRACKER] is_market_regime_bullish check error: {e}")
        return True, "Market check skipped on exception."
    finally:
        cur.close()
        if close_conn:
            conn.close()

