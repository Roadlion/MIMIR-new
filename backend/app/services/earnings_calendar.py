# backend/app/services/earnings_calendar.py
"""
MIMIR Earnings Calendar
========================
Fetches and maintains a local earnings calendar for all tracked tickers.
Uses yfinance as the primary source (free, no API key).

Table: yggdrasil.mimir_earnings_calendar
  ticker, earnings_date, earnings_time (BMO/AMC/TNS), estimated_eps,
  actual_eps, surprise_pct, confirmed, source, fetched_at

Public API:
  refresh_earnings_calendar(tickers)   → upserts upcoming dates
  get_upcoming_earnings(days_ahead=10) → list of {ticker, date, time}
  is_pre_earnings_window(ticker, min_days=3, max_days=10) → bool
  get_earnings_surprise(ticker)        → float | None (last surprise %)
"""

import logging
from datetime import datetime, timedelta, timezone, date
from typing import List, Dict, Optional

import yfinance as yf

from ..database import get_db_connection
from ..config import get_settings
from ..utils.ticker_validator import is_yfinance_compatible, filter_yfinance_tickers

logger = logging.getLogger(__name__)
settings = get_settings()
_SCHEMA = settings.mimir_schema


# ─────────────────────────────────────────────────────────────────────────────
# Table bootstrap
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_table(conn):
    """Auto-create mimir_earnings_calendar if it doesn't exist."""
    try:
        cur = conn.cursor()
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {_SCHEMA}.mimir_earnings_calendar (
                id              SERIAL PRIMARY KEY,
                ticker          TEXT NOT NULL,
                company_name    TEXT,
                earnings_date   DATE NOT NULL,
                earnings_time   TEXT DEFAULT 'TNS',
                estimated_eps   FLOAT,
                actual_eps      FLOAT,
                surprise_pct    FLOAT,
                confirmed       BOOLEAN DEFAULT FALSE,
                source          TEXT DEFAULT 'yfinance',
                fetched_at      TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(ticker, earnings_date)
            )
        """)
        # Index for fast upcoming-earnings queries
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_earnings_cal_date
            ON {_SCHEMA}.mimir_earnings_calendar(earnings_date)
        """)
        conn.commit()
        cur.close()
    except Exception as e:
        conn.rollback()
        logger.warning(f"[EARNINGS_CAL] Table ensure failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Data fetching
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_earnings_date_yf(ticker: str) -> Optional[Dict]:
    """
    Pulls the next earnings date for a ticker from yfinance.
    Returns None if unavailable or if ticker is not an equity.
    """
    try:
        info = yf.Ticker(ticker).calendar
        if info is None or (hasattr(info, "empty") and info.empty):
            return None

        # yfinance returns a DataFrame with columns like 'Earnings Date', 'EPS Estimate', etc.
        # Depending on yf version it can be a dict or a DataFrame.
        if hasattr(info, "to_dict"):
            info = info.to_dict()

        # Extract earnings date
        earnings_date_raw = None
        for key in ("Earnings Date", "Earnings Dates", "earnings_date"):
            if key in info:
                val = info[key]
                if hasattr(val, "__iter__") and not isinstance(val, str):
                    val = list(val)[0] if val else None
                earnings_date_raw = val
                break

        if earnings_date_raw is None:
            return None

        # Normalize to date
        if hasattr(earnings_date_raw, "date"):
            earnings_date = earnings_date_raw.date()
        elif isinstance(earnings_date_raw, str):
            earnings_date = datetime.strptime(earnings_date_raw[:10], "%Y-%m-%d").date()
        else:
            earnings_date = date.fromisoformat(str(earnings_date_raw)[:10])

        # Skip if already in the past (> 1 day ago)
        if earnings_date < (datetime.utcnow().date() - timedelta(days=1)):
            return None

        # Earnings time: BMO / AMC / TNS
        earnings_time = "TNS"
        for key in ("Earnings Timing", "Earnings Time"):
            if key in info and info[key]:
                raw_t = str(info[key]).upper()
                if "BEFORE" in raw_t or "BMO" in raw_t or "MORNING" in raw_t:
                    earnings_time = "BMO"
                elif "AFTER" in raw_t or "AMC" in raw_t or "CLOSE" in raw_t:
                    earnings_time = "AMC"
                break

        # EPS estimate
        eps_est = None
        for key in ("EPS Estimate", "Earnings Estimate", "epsEstimate"):
            if key in info and info[key] is not None:
                try:
                    eps_est = float(info[key])
                except (TypeError, ValueError):
                    pass
                break

        return {
            "ticker": ticker.upper(),
            "earnings_date": earnings_date,
            "earnings_time": earnings_time,
            "estimated_eps": eps_est,
            "source": "yfinance",
        }

    except Exception as e:
        logger.debug(f"[EARNINGS_CAL] yfinance fetch failed for {ticker}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def refresh_earnings_calendar(tickers: List[str], conn=None) -> int:
    """
    Fetches upcoming earnings dates for the given tickers and upserts
    them into mimir_earnings_calendar.
    Returns the number of rows upserted.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    _ensure_table(conn)
    cur = conn.cursor()
    upserted = 0

    # Filter out ISIN-format and structured product tickers before hitting yfinance
    valid_tickers = filter_yfinance_tickers(tickers)
    skipped = len(tickers) - len(valid_tickers)
    if skipped > 0:
        logger.debug(f"[EARNINGS_CAL] Skipped {skipped} non-equity tickers (ISINs/structured products).")

    for ticker in valid_tickers:
        try:
            result = _fetch_earnings_date_yf(ticker)
            if not result:
                continue

            cur.execute(f"""
                INSERT INTO {_SCHEMA}.mimir_earnings_calendar
                    (ticker, earnings_date, earnings_time, estimated_eps, confirmed, source, fetched_at)
                VALUES (%s, %s, %s, %s, FALSE, %s, NOW())
                ON CONFLICT (ticker, earnings_date) DO UPDATE
                    SET earnings_time  = EXCLUDED.earnings_time,
                        estimated_eps  = COALESCE(EXCLUDED.estimated_eps, {_SCHEMA}.mimir_earnings_calendar.estimated_eps),
                        source         = EXCLUDED.source,
                        fetched_at     = NOW()
            """, (
                result["ticker"],
                result["earnings_date"],
                result["earnings_time"],
                result["estimated_eps"],
                result["source"],
            ))
            upserted += cur.rowcount

        except Exception as e:
            logger.warning(f"[EARNINGS_CAL] Upsert failed for {ticker}: {e}")
            conn.rollback()
            # Re-establish cursor after rollback
            cur = conn.cursor()
            continue

    try:
        conn.commit()
    except Exception as e:
        logger.error(f"[EARNINGS_CAL] Final commit failed: {e}")
        conn.rollback()

    cur.close()
    if close_conn:
        conn.close()

    logger.info(f"[EARNINGS_CAL] Refreshed {upserted} earnings dates for {len(tickers)} tickers.")
    return upserted


def get_upcoming_earnings(days_ahead: int = 14, conn=None) -> List[Dict]:
    """
    Returns a list of upcoming earnings events within the next N days.
    Each item: {ticker, earnings_date, earnings_time, days_until, estimated_eps}
    Sorted by earnings_date ASC.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    _ensure_table(conn)
    cur = conn.cursor()
    today = datetime.utcnow().date()
    until = today + timedelta(days=days_ahead)

    try:
        cur.execute(f"""
            SELECT ticker, company_name, earnings_date, earnings_time,
                   estimated_eps, actual_eps, surprise_pct, confirmed, source
            FROM {_SCHEMA}.mimir_earnings_calendar
            WHERE earnings_date >= %s AND earnings_date <= %s
            ORDER BY earnings_date ASC
        """, (today, until))
        rows = cur.fetchall()
        result = []
        for row in rows:
            earnings_date = row[2]
            days_until = (earnings_date - today).days
            result.append({
                "ticker":        row[0],
                "company_name":  row[1],
                "earnings_date": earnings_date.isoformat(),
                "earnings_time": row[3],
                "days_until":    days_until,
                "estimated_eps": float(row[4]) if row[4] is not None else None,
                "actual_eps":    float(row[5]) if row[5] is not None else None,
                "surprise_pct":  float(row[6]) if row[6] is not None else None,
                "confirmed":     row[7],
                "source":        row[8],
            })
        return result
    except Exception as e:
        logger.error(f"[EARNINGS_CAL] get_upcoming_earnings error: {e}")
        return []
    finally:
        cur.close()
        if close_conn:
            conn.close()


def is_pre_earnings_window(ticker: str, min_days: int = 3, max_days: int = 10, conn=None) -> bool:
    """
    Returns True if this ticker has a confirmed upcoming earnings date
    between min_days and max_days from today.
    This is the gate used by scan_pre_earnings_catalysts to prevent false signals.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    _ensure_table(conn)
    cur = conn.cursor()
    today = datetime.utcnow().date()
    earliest = today + timedelta(days=min_days)
    latest = today + timedelta(days=max_days)

    try:
        cur.execute(f"""
            SELECT 1 FROM {_SCHEMA}.mimir_earnings_calendar
            WHERE ticker = %s
              AND earnings_date >= %s
              AND earnings_date <= %s
            LIMIT 1
        """, (ticker.upper(), earliest, latest))
        return cur.fetchone() is not None
    except Exception as e:
        logger.error(f"[EARNINGS_CAL] is_pre_earnings_window error for {ticker}: {e}")
        return False
    finally:
        cur.close()
        if close_conn:
            conn.close()


def get_earnings_surprise(ticker: str, conn=None) -> Optional[float]:
    """
    Returns the most recent historical EPS surprise percentage for a ticker.
    Used by the learning loop to calibrate pre-earnings signal conviction.
    Positive = beat, negative = miss.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    _ensure_table(conn)
    cur = conn.cursor()

    try:
        cur.execute(f"""
            SELECT surprise_pct FROM {_SCHEMA}.mimir_earnings_calendar
            WHERE ticker = %s
              AND actual_eps IS NOT NULL
              AND surprise_pct IS NOT NULL
            ORDER BY earnings_date DESC
            LIMIT 1
        """, (ticker.upper(),))
        row = cur.fetchone()
        return float(row[0]) if row else None
    except Exception as e:
        logger.error(f"[EARNINGS_CAL] get_earnings_surprise error for {ticker}: {e}")
        return None
    finally:
        cur.close()
        if close_conn:
            conn.close()


def record_actual_earnings(ticker: str, earnings_date: date, actual_eps: float, conn=None) -> bool:
    """
    Records actual EPS after earnings release and computes surprise %.
    Called from the performance evaluator after earnings date passes.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    _ensure_table(conn)
    cur = conn.cursor()
    try:
        cur.execute(f"""
            UPDATE {_SCHEMA}.mimir_earnings_calendar
            SET actual_eps   = %s,
                surprise_pct = CASE
                    WHEN estimated_eps IS NOT NULL AND estimated_eps != 0
                    THEN ((%s - estimated_eps) / ABS(estimated_eps)) * 100.0
                    ELSE NULL
                END,
                confirmed = TRUE
            WHERE ticker = %s AND earnings_date = %s
        """, (actual_eps, actual_eps, ticker.upper(), earnings_date))
        conn.commit()
        return cur.rowcount > 0
    except Exception as e:
        conn.rollback()
        logger.error(f"[EARNINGS_CAL] record_actual_earnings error for {ticker}: {e}")
        return False
    finally:
        cur.close()
        if close_conn:
            conn.close()
