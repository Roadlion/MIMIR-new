# backend/app/analytics/narrative_tracker.py
"""
MIMIR Narrative Age & Phase Tracker
=====================================
Tracks how "fresh" a ticker's news narrative is and classifies it into
one of four lifecycle phases:

  EMERGING  (0–12h,  rising velocity)  → Best entry window
  BUILDING  (12–48h, sustained)        → Valid entry, tighter stops
  PEAK      (>48h,   velocity slowing) → Skip new entries
  FADING    (velocity negative)        → Exit signal for open positions

Why this matters:
  A sentiment score of +0.6 on a story that's been circulating for 5 days
  is worth far less than +0.4 on a story that broke 2 hours ago.
  Current MIMIR treats them identically — this module fixes that.

Public API:
  get_narrative_phase(ticker) → NarrativeState dataclass
  is_narrative_enterable(ticker) → bool  (EMERGING or BUILDING)
  is_narrative_fading(ticker) → bool     (FADING)
  get_all_narrative_states(tickers) → Dict[str, NarrativeState]
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from ..database import get_db_connection
from ..config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
_SCHEMA = settings.mimir_schema


# ─────────────────────────────────────────────────────────────────────────────
# Phase constants
# ─────────────────────────────────────────────────────────────────────────────

EMERGING_HOURS  = 12    # Story < 12h old
BUILDING_HOURS  = 48    # Story 12–48h old
# Beyond 48h → PEAK if velocity still positive, FADING if velocity negative


@dataclass
class NarrativeState:
    ticker:               str
    phase:                str         # EMERGING | BUILDING | PEAK | FADING | UNKNOWN
    narrative_age_hours:  float       # Hours since first article in this narrative window
    freshness_score:      float       # 1.0 = just broke, 0.0 = stale/fading
    article_count_24h:    int
    article_count_total:  int         # Over the narrative window
    avg_sentiment:        float
    sent_velocity:        float       # Change in avg sentiment per day
    first_article_ts:     Optional[datetime]
    latest_article_ts:    Optional[datetime]
    attention_decay:      float       # From sentiment_momentum (0–1, high = fading)
    entry_ok:             bool        # True if EMERGING or BUILDING
    exit_flag:            bool        # True if FADING


def _compute_narrative_state(ticker: str, conn) -> NarrativeState:
    """
    Queries mimir_raw_articles + mimir_sentiment_impacts to determine
    the narrative lifecycle phase for a ticker.
    """
    cur = conn.cursor()
    now = datetime.now(timezone.utc)

    # Define the narrative window: look back up to 7 days
    window_start = now - timedelta(days=7)

    try:
        # 1. Get all articles mentioning this ticker in the last 7 days
        cur.execute(f"""
            SELECT a.published_ts, si.sentiment_score, si.confidence
            FROM {_SCHEMA}.mimir_raw_articles a
            JOIN {_SCHEMA}.mimir_sentiment_impacts si ON si.article_id = a.id
            WHERE si.ticker = %s
              AND a.published_ts >= %s
            ORDER BY a.published_ts ASC
        """, (ticker.upper(), window_start))
        rows = cur.fetchall()

        if not rows:
            return NarrativeState(
                ticker=ticker, phase="UNKNOWN",
                narrative_age_hours=0, freshness_score=0,
                article_count_24h=0, article_count_total=0,
                avg_sentiment=0, sent_velocity=0,
                first_article_ts=None, latest_article_ts=None,
                attention_decay=0.5, entry_ok=False, exit_flag=False,
            )

        timestamps = [r[0] for r in rows]
        sentiments = [float(r[1]) for r in rows]

        first_ts = timestamps[0]
        latest_ts = timestamps[-1]

        # Ensure timezone-aware
        if first_ts.tzinfo is None:
            first_ts = first_ts.replace(tzinfo=timezone.utc)
        if latest_ts.tzinfo is None:
            latest_ts = latest_ts.replace(tzinfo=timezone.utc)

        narrative_age_hours = (now - first_ts).total_seconds() / 3600.0

        # 2. Articles in last 24h vs prior 24h (for velocity)
        cutoff_24h = now - timedelta(hours=24)
        cutoff_48h = now - timedelta(hours=48)

        rows_24h = [r for r in rows if r[0] >= cutoff_24h or (r[0].replace(tzinfo=timezone.utc) if r[0].tzinfo is None else r[0]) >= cutoff_24h]
        rows_prev_24h = [r for r in rows if (r[0].replace(tzinfo=timezone.utc) if r[0].tzinfo is None else r[0]) < cutoff_24h
                         and (r[0].replace(tzinfo=timezone.utc) if r[0].tzinfo is None else r[0]) >= cutoff_48h]

        count_24h = len(rows_24h)
        count_prev = len(rows_prev_24h)

        # Sentiment velocity: change in avg sentiment between windows
        if rows_24h and rows_prev_24h:
            avg_sent_24h = sum(float(r[1]) for r in rows_24h) / len(rows_24h)
            avg_sent_prev = sum(float(r[1]) for r in rows_prev_24h) / len(rows_prev_24h)
            sent_velocity = avg_sent_24h - avg_sent_prev
        elif rows_24h:
            avg_sent_24h = sum(float(r[1]) for r in rows_24h) / len(rows_24h)
            sent_velocity = avg_sent_24h  # No prior data = treat as fresh positive signal
        else:
            avg_sent_24h = 0.0
            sent_velocity = 0.0

        avg_sentiment = sum(sentiments) / len(sentiments)

        # 3. Attention decay: ratio of recent to total articles (high = fading)
        total = len(rows)
        if total > 0 and count_24h < total:
            attention_decay = 1.0 - (count_24h / total)
        else:
            attention_decay = 0.0  # All articles are recent = active

        # 4. Phase classification
        if narrative_age_hours < EMERGING_HOURS and sent_velocity >= 0:
            phase = "EMERGING"
            freshness_score = 1.0
        elif narrative_age_hours < BUILDING_HOURS and sent_velocity >= -0.05:
            phase = "BUILDING"
            freshness_score = max(0.5, 1.0 - (narrative_age_hours / BUILDING_HOURS) * 0.5)
        elif sent_velocity < -0.05 or attention_decay > 0.7:
            phase = "FADING"
            freshness_score = max(0.0, 0.3 - attention_decay * 0.3)
        else:
            phase = "PEAK"
            freshness_score = max(0.1, 0.5 - (narrative_age_hours / 100.0) * 0.4)

        entry_ok = phase in ("EMERGING", "BUILDING")
        exit_flag = phase == "FADING"

        return NarrativeState(
            ticker=ticker,
            phase=phase,
            narrative_age_hours=round(narrative_age_hours, 1),
            freshness_score=round(freshness_score, 3),
            article_count_24h=count_24h,
            article_count_total=total,
            avg_sentiment=round(avg_sentiment, 4),
            sent_velocity=round(sent_velocity, 4),
            first_article_ts=first_ts,
            latest_article_ts=latest_ts,
            attention_decay=round(attention_decay, 3),
            entry_ok=entry_ok,
            exit_flag=exit_flag,
        )

    except Exception as e:
        logger.error(f"[NARRATIVE] Error computing state for {ticker}: {e}")
        return NarrativeState(
            ticker=ticker, phase="UNKNOWN",
            narrative_age_hours=0, freshness_score=0,
            article_count_24h=0, article_count_total=0,
            avg_sentiment=0, sent_velocity=0,
            first_article_ts=None, latest_article_ts=None,
            attention_decay=0.5, entry_ok=False, exit_flag=False,
        )
    finally:
        cur.close()


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def get_narrative_phase(ticker: str, conn=None) -> NarrativeState:
    """
    Returns the full narrative lifecycle state for a single ticker.
    Cheap — single DB query.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    try:
        return _compute_narrative_state(ticker, conn)
    finally:
        if close_conn:
            conn.close()


def is_narrative_enterable(ticker: str, conn=None) -> bool:
    """
    Returns True if the narrative is in EMERGING or BUILDING phase.
    Used as a gate in signal_fusion — PEAK/FADING narratives don't fire new entries.
    """
    state = get_narrative_phase(ticker, conn=conn)
    if state.phase == "UNKNOWN":
        return True  # No data = don't block (let other gates decide)
    return state.entry_ok


def is_narrative_fading(ticker: str, conn=None) -> bool:
    """
    Returns True if narrative is FADING — useful for surfacing exit signals
    on open positions. Called by performance evaluator.
    """
    state = get_narrative_phase(ticker, conn=conn)
    return state.exit_flag


def get_all_narrative_states(tickers: List[str], conn=None) -> Dict[str, NarrativeState]:
    """
    Batch compute narrative states for all given tickers using a single connection.
    Used by scan_all_tickers to avoid N+1 connection overhead.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    results = {}
    try:
        for ticker in tickers:
            results[ticker] = _compute_narrative_state(ticker, conn)
    finally:
        if close_conn:
            conn.close()

    return results


def format_narrative_for_discord(state: NarrativeState) -> str:
    """
    Returns a compact narrative summary string for Discord alert embedding.
    """
    phase_emoji = {
        "EMERGING": "🌱",
        "BUILDING": "📈",
        "PEAK":     "⚠️",
        "FADING":   "📉",
        "UNKNOWN":  "❓",
    }
    emoji = phase_emoji.get(state.phase, "❓")
    age_str = (
        f"{state.narrative_age_hours:.0f}h"
        if state.narrative_age_hours < 48
        else f"{state.narrative_age_hours / 24:.1f}d"
    )
    return (
        f"{emoji} Narrative Phase: **{state.phase}** | "
        f"Age: {age_str} | "
        f"Articles (24h): {state.article_count_24h} | "
        f"Freshness: {state.freshness_score:.0%} | "
        f"Velocity: {state.sent_velocity:+.2f}"
    )


def get_fading_open_positions(conn=None) -> List[Dict]:
    """
    Returns a list of open trade signal tickers whose narratives are FADING.
    Used to surface exit recommendations for open positions.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT DISTINCT ticker FROM {_SCHEMA}.mimir_trade_signals
            WHERE status = 'ACTIVE'
              AND signal_type = 'BUY'
        """)
        open_tickers = [row[0] for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"[NARRATIVE] get_fading_open_positions query error: {e}")
        return []
    finally:
        cur.close()

    if not open_tickers:
        if close_conn:
            conn.close()
        return []

    fading = []
    for ticker in open_tickers:
        state = _compute_narrative_state(ticker, conn)
        if state.exit_flag:
            fading.append({
                "ticker":            ticker,
                "phase":             state.phase,
                "narrative_age_h":   state.narrative_age_hours,
                "attention_decay":   state.attention_decay,
                "avg_sentiment":     state.avg_sentiment,
                "sent_velocity":     state.sent_velocity,
                "exit_recommendation": (
                    f"Narrative is {state.phase} after {state.narrative_age_hours:.0f}h. "
                    f"Attention decay: {state.attention_decay:.0%}. "
                    f"Consider reducing or closing position."
                ),
            })

    if close_conn:
        conn.close()

    return fading
