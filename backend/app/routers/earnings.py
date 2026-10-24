# backend/app/routers/earnings.py
"""
MIMIR Earnings Calendar Router
================================
GET /api/v1/earnings/upcoming        → upcoming earnings in next 14 days
GET /api/v1/earnings/ticker/{ticker} → single ticker earnings window check
POST /api/v1/earnings/refresh        → manually trigger calendar refresh
GET /api/v1/earnings/fading          → open positions with fading narratives
"""

import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Query

from ..services.earnings_calendar import (
    get_upcoming_earnings,
    is_pre_earnings_window,
    refresh_earnings_calendar,
    get_earnings_surprise,
)
from ..analytics.narrative_tracker import get_fading_open_positions, get_narrative_phase
from ..analytics.surge_detector import get_active_surges
from ..analytics.signal_fusion import DEFAULT_TICKERS
from ..database import get_db_connection
from ..config import get_settings

router = APIRouter(prefix="/api/v1/earnings", tags=["earnings"])
settings = get_settings()
logger = logging.getLogger(__name__)


@router.get("/upcoming")
async def upcoming_earnings(days: int = Query(default=14, ge=1, le=60)):
    """
    Returns all tracked tickers with earnings scheduled in the next N days.
    Sorted by earnings_date ascending.
    """
    try:
        events = get_upcoming_earnings(days_ahead=days)
        return {
            "status": "ok",
            "days_ahead": days,
            "count": len(events),
            "earnings": events,
        }
    except Exception as e:
        logger.error(f"[EARNINGS_ROUTER] upcoming_earnings error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ticker/{ticker}")
async def ticker_earnings_info(ticker: str):
    """
    Returns earnings window status and narrative phase for a single ticker.
    Useful for the Oracle to answer "is {ticker} reporting soon?"
    """
    ticker = ticker.strip().upper()
    try:
        in_window = is_pre_earnings_window(ticker, min_days=1, max_days=14)
        upcoming = get_upcoming_earnings(days_ahead=30)
        ticker_events = [e for e in upcoming if e["ticker"] == ticker]
        surprise = get_earnings_surprise(ticker)
        narrative = get_narrative_phase(ticker)

        return {
            "ticker": ticker,
            "in_pre_earnings_window": in_window,
            "upcoming_earnings": ticker_events,
            "last_eps_surprise_pct": surprise,
            "narrative_phase": narrative.phase,
            "narrative_age_hours": narrative.narrative_age_hours,
            "narrative_freshness": narrative.freshness_score,
            "narrative_entry_ok": narrative.entry_ok,
        }
    except Exception as e:
        logger.error(f"[EARNINGS_ROUTER] ticker_earnings_info error for {ticker}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/refresh")
async def refresh_calendar():
    """
    Manually triggers an earnings calendar refresh for all tracked tickers.
    Normally runs automatically every 24h via background_worker.
    """
    try:
        # Get all tickers we track
        conn = get_db_connection()
        cur = conn.cursor()
        tickers = set(DEFAULT_TICKERS)
        try:
            cur.execute(
                f"SELECT DISTINCT ticker FROM {settings.mimir_schema}.mimir_dynamic_tickers "
                f"WHERE ticker IS NOT NULL"
            )
            for row in cur.fetchall():
                tickers.add(row[0].strip().upper())
        except Exception:
            pass
        cur.close()
        conn.close()

        # Run refresh
        n = refresh_earnings_calendar(list(tickers))
        return {
            "status": "ok",
            "tickers_scanned": len(tickers),
            "dates_upserted": n,
        }
    except Exception as e:
        logger.error(f"[EARNINGS_ROUTER] refresh_calendar error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/fading")
async def fading_positions():
    """
    Returns open BUY positions where the narrative is FADING.
    Useful for timing exits on positions before retail also exits.
    """
    try:
        fading = get_fading_open_positions()
        return {
            "status": "ok",
            "count": len(fading),
            "fading_positions": fading,
        }
    except Exception as e:
        logger.error(f"[EARNINGS_ROUTER] fading_positions error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/surges")
async def active_surges(days: int = Query(default=3, ge=1, le=7)):
    """
    Returns recent social volume surge events.
    Shows tickers where retail FOMO wave is building but price hasn't moved yet.
    """
    try:
        surges = get_active_surges(days_back=days)
        return {
            "status": "ok",
            "days_back": days,
            "count": len(surges),
            "surges": surges,
        }
    except Exception as e:
        logger.error(f"[EARNINGS_ROUTER] active_surges error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
