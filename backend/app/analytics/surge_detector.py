# backend/app/analytics/surge_detector.py
"""
MIMIR Social Volume Surge Detector
====================================
Detects tickers where social mention volume has spiked significantly
above their 30-day baseline BUT price hasn't moved yet — meaning retail
interest is forming before purchases happen.

This is MIMIR's primary "front-run retail FOMO" signal.

Logic:
  1. Group mimir_social_chatter by (ticker, date) → daily mention counts
  2. Compute 30-day rolling baseline per ticker
  3. Flag tickers where today's count > SURGE_THRESHOLD × baseline
  4. Cross-check price: if |price_change_1d| < PRICE_ALREADY_MOVED_PCT,
     the surge hasn't been priced in yet → SURGE_PENDING
  5. Write to mimir_social_surges table
  6. Emit Discord alert + create BUY signal if sentiment is also positive

Table: yggdrasil.mimir_social_surges
  ticker, surge_date, baseline_volume, surge_volume, surge_ratio,
  avg_sentiment, price_change_1d, status, created_at

Status values: PENDING | PRICED_IN | EXPIRED | FIRED
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

from ..database import get_db_connection
from ..config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
_SCHEMA = settings.mimir_schema

# ── Thresholds ────────────────────────────────────────────────────────────────
SURGE_THRESHOLD       = 3.0   # today's volume must be 3x the baseline
MIN_BASELINE_DAYS     = 7     # need at least 7 days of history to compute baseline
MIN_SURGE_VOLUME      = 5     # at least 5 mentions today (avoid 1→3 noise)
PRICE_ALREADY_MOVED   = 0.03  # 3%: if price already moved this much, skip
MIN_SENTIMENT_FOR_BUY = 0.15  # require positive sentiment to fire BUY surge signal


def _ensure_surge_table(conn):
    """Auto-create mimir_social_surges if missing."""
    try:
        cur = conn.cursor()
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {_SCHEMA}.mimir_social_surges (
                id              SERIAL PRIMARY KEY,
                ticker          TEXT NOT NULL,
                surge_date      DATE NOT NULL,
                baseline_volume FLOAT,
                surge_volume    INT,
                surge_ratio     FLOAT,
                avg_sentiment   FLOAT,
                price_change_1d FLOAT,
                status          TEXT DEFAULT 'PENDING',
                signal_id       INT,
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(ticker, surge_date)
            )
        """)
        conn.commit()
        cur.close()
    except Exception as e:
        conn.rollback()
        logger.warning(f"[SURGE] Table ensure failed: {e}")


def _get_daily_social_volumes(conn, days_lookback: int = 35) -> Dict[str, List[Dict]]:
    """
    Returns a dict of ticker → list of {date, count, avg_sentiment}
    for the past N days, sourced from mimir_social_chatter.

    Note: mimir_social_chatter stores hourly bucket aggregations, not individual posts.
    - bucket_ts is the timestamp column (not published_ts)
    - post_count is the actual number of posts per bucket (not COUNT(*) of rows)
    """
    start_date = (datetime.utcnow() - timedelta(days=days_lookback)).date()
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT
                ticker,
                (bucket_ts AT TIME ZONE 'UTC')::date AS chat_date,
                SUM(post_count) AS mention_count,
                AVG(sentiment_score) AS avg_sentiment
            FROM {_SCHEMA}.mimir_social_chatter
            WHERE bucket_ts >= %s
              AND ticker IS NOT NULL
            GROUP BY ticker, chat_date
            ORDER BY ticker, chat_date
        """, (start_date,))
        rows = cur.fetchall()
    except Exception as e:
        logger.error(f"[SURGE] Social volume query failed: {e}")
        return {}
    finally:
        cur.close()

    result: Dict[str, List[Dict]] = {}
    for ticker, chat_date, count, avg_sent in rows:
        if ticker not in result:
            result[ticker] = []
        result[ticker].append({
            "date": chat_date,
            "count": int(count),
            "avg_sentiment": float(avg_sent) if avg_sent is not None else 0.0,
        })
    return result


def _get_price_change_1d(ticker: str, conn) -> Optional[float]:
    """Returns yesterday-to-today price change as a fraction (0.03 = 3%)."""
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT close FROM {_SCHEMA}.v_mimir_daily_ohlcv
            WHERE ticker = %s
            ORDER BY date DESC
            LIMIT 2
        """, (ticker.upper(),))
        rows = cur.fetchall()
        if len(rows) < 2:
            return None
        today_close = float(rows[0][0])
        prev_close = float(rows[1][0])
        return (today_close - prev_close) / (prev_close + 1e-10)
    except Exception as e:
        logger.debug(f"[SURGE] Price change lookup failed for {ticker}: {e}")
        return None
    finally:
        cur.close()


def detect_social_surges(conn=None) -> List[Dict]:
    """
    Main detection function. Scans all tracked tickers for social volume surges
    where the price hasn't moved yet (SURGE_PENDING condition).

    Returns list of surge event dicts.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    _ensure_surge_table(conn)

    today = datetime.utcnow().date()
    social_data = _get_daily_social_volumes(conn, days_lookback=35)
    surge_events = []

    for ticker, daily_rows in social_data.items():
        try:
            # Find today's row
            today_rows = [r for r in daily_rows if r["date"] == today]
            if not today_rows:
                continue
            today_count = today_rows[0]["count"]
            today_sentiment = today_rows[0]["avg_sentiment"]

            if today_count < MIN_SURGE_VOLUME:
                continue

            # Compute 30-day baseline (exclude today)
            baseline_rows = [r for r in daily_rows if r["date"] < today]
            if len(baseline_rows) < MIN_BASELINE_DAYS:
                continue

            baseline_avg = sum(r["count"] for r in baseline_rows) / len(baseline_rows)
            if baseline_avg < 1.0:
                baseline_avg = 1.0  # prevent division by zero

            surge_ratio = today_count / baseline_avg
            if surge_ratio < SURGE_THRESHOLD:
                continue

            # Cross-check price hasn't already moved
            price_change = _get_price_change_1d(ticker, conn)
            if price_change is not None and abs(price_change) >= PRICE_ALREADY_MOVED:
                logger.info(
                    f"[SURGE] {ticker}: surge ratio {surge_ratio:.1f}x but price already moved "
                    f"{price_change * 100:.1f}% — skipping (priced in)."
                )
                continue

            # Log the event
            logger.info(
                f"[SURGE] 🚨 {ticker}: Social volume {today_count} vs baseline {baseline_avg:.1f} "
                f"({surge_ratio:.1f}x) | Sentiment: {today_sentiment:+.2f} | "
                f"Price change: {(price_change or 0) * 100:.1f}%"
            )

            surge_events.append({
                "ticker":          ticker,
                "surge_date":      today,
                "baseline_volume": round(baseline_avg, 2),
                "surge_volume":    today_count,
                "surge_ratio":     round(surge_ratio, 2),
                "avg_sentiment":   round(today_sentiment, 4),
                "price_change_1d": round(price_change, 4) if price_change is not None else None,
                "status":          "PENDING",
            })

        except Exception as ticker_err:
            logger.warning(f"[SURGE] Error processing {ticker}: {ticker_err}")
            continue

    # Upsert all surge events
    if surge_events:
        _upsert_surge_events(surge_events, conn)

    if close_conn:
        conn.close()

    return surge_events


def _upsert_surge_events(events: List[Dict], conn):
    """Upserts surge events into mimir_social_surges."""
    cur = conn.cursor()
    try:
        for ev in events:
            cur.execute(f"""
                INSERT INTO {_SCHEMA}.mimir_social_surges
                    (ticker, surge_date, baseline_volume, surge_volume,
                     surge_ratio, avg_sentiment, price_change_1d, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ticker, surge_date) DO UPDATE
                    SET surge_volume    = EXCLUDED.surge_volume,
                        surge_ratio     = EXCLUDED.surge_ratio,
                        avg_sentiment   = EXCLUDED.avg_sentiment,
                        price_change_1d = EXCLUDED.price_change_1d
            """, (
                ev["ticker"], ev["surge_date"], ev["baseline_volume"],
                ev["surge_volume"], ev["surge_ratio"], ev["avg_sentiment"],
                ev["price_change_1d"], ev["status"],
            ))
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"[SURGE] Upsert failed: {e}")
    finally:
        cur.close()


def fire_surge_signals(surge_events: List[Dict], conn=None) -> int:
    """
    For each PENDING surge with positive sentiment that passes the minimum threshold,
    creates a BUY trade signal and sends a Discord alert.
    Returns number of signals fired.
    """
    if not surge_events:
        return 0

    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    fired = 0
    for ev in surge_events:
        ticker = ev["ticker"]
        sentiment = ev["avg_sentiment"]
        surge_ratio = ev["surge_ratio"]

        if sentiment < MIN_SENTIMENT_FOR_BUY:
            logger.info(
                f"[SURGE] {ticker}: surge detected but sentiment too low "
                f"({sentiment:+.2f}) — no signal fired."
            )
            continue

        try:
            from .signal_fusion import insert_trade_signal, check_duplicate_signal, get_recent_prices
            from ..services.discord_notifier import send_trade_alert as _discord_alert

            if check_duplicate_signal(ticker, "BUY", conn=conn):
                logger.info(f"[SURGE] {ticker}: duplicate signal suppressed.")
                continue

            # Get current price for signal entry
            df = get_recent_prices(ticker, days=5, conn=conn)
            if df is None or df.empty:
                continue

            current_price = float(df["close"].iloc[-1])
            rsi_val = float(df["rsi"].iloc[-1]) if "rsi" in df.columns else 50.0
            support = float(df["support"].iloc[-1]) if "support" in df.columns and not df["support"].isna().iloc[-1] else current_price * 0.97
            resistance = float(df["resistance"].iloc[-1]) if "resistance" in df.columns and not df["resistance"].isna().iloc[-1] else current_price * 1.04

            reason = (
                f"SOCIAL_SURGE: Mention volume {ev['surge_volume']}x vs baseline "
                f"{ev['baseline_volume']:.0f} ({surge_ratio:.1f}x surge) | "
                f"Avg sentiment: {sentiment:+.2f} | "
                f"Price unchanged — retail FOMO wave incoming. "
                f"Entry before retail reaction."
            )

            # Build thesis
            thesis = (
                f"[Social Volume Surge — Pre-FOMO Entry]\n"
                f"Ticker: {ticker}\n"
                f"Today's social mentions: {ev['surge_volume']} vs 30-day baseline {ev['baseline_volume']:.0f} ({surge_ratio:.1f}x above normal)\n"
                f"Average community sentiment: {sentiment:+.2f}\n"
                f"Price change today: {(ev.get('price_change_1d') or 0) * 100:.1f}% — surge not yet priced in.\n\n"
                f"This signal fires BEFORE the retail purchasing wave. "
                f"Social momentum is building but institutions and casual investors "
                f"have not acted yet. Exit once attention_decay_ratio rises above 0.6."
            )

            success = insert_trade_signal(
                ticker=ticker,
                signal_type="BUY",
                trigger_price=current_price,
                rsi=rsi_val,
                sentiment=sentiment,
                support=support,
                resistance=resistance,
                reason=reason,
                investment_thesis=thesis,
                conviction_score=round(min(surge_ratio / 10.0, 1.0), 3),
                conn=conn,
            )

            if success:
                fired += 1
                logger.info(f"[SURGE] ✅ BUY signal fired for {ticker} (surge {surge_ratio:.1f}x)")

                # Discord notification
                try:
                    _discord_alert(
                        ticker=ticker,
                        signal_type="BUY",
                        trigger_price=current_price,
                        target_price=resistance,
                        stop_loss=support,
                        sentiment_score=sentiment,
                        conviction_score=min(surge_ratio / 10.0, 1.0),
                        reasoning=reason[:300],
                        investment_thesis=thesis[:500],
                        alert_source="SOCIAL_SURGE",
                    )
                except Exception as dc_err:
                    logger.warning(f"[SURGE] Discord alert failed for {ticker}: {dc_err}")

                # Update status
                try:
                    cur = conn.cursor()
                    cur.execute(f"""
                        UPDATE {_SCHEMA}.mimir_social_surges
                        SET status = 'FIRED'
                        WHERE ticker = %s AND surge_date = %s
                    """, (ticker, ev["surge_date"]))
                    conn.commit()
                    cur.close()
                except Exception:
                    pass

        except Exception as sig_err:
            logger.error(f"[SURGE] Signal generation failed for {ticker}: {sig_err}")
            continue

    if close_conn:
        conn.close()

    return fired


def run_surge_detection_cycle(conn=None) -> int:
    """
    Full cycle: detect surges → fire BUY signals for qualifying ones.
    Called by background_worker every scan cycle.
    Returns total signals fired.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    try:
        surge_events = detect_social_surges(conn=conn)
        if not surge_events:
            return 0
        fired = fire_surge_signals(surge_events, conn=conn)
        logger.info(f"[SURGE] Cycle complete — {len(surge_events)} surges detected, {fired} signals fired.")
        return fired
    finally:
        if close_conn:
            conn.close()


def get_active_surges(days_back: int = 3, conn=None) -> List[Dict]:
    """
    Returns recent surge events for display in Oracle / trade alerts UI.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    _ensure_surge_table(conn)
    cur = conn.cursor()
    since = (datetime.utcnow() - timedelta(days=days_back)).date()

    try:
        cur.execute(f"""
            SELECT ticker, surge_date, baseline_volume, surge_volume,
                   surge_ratio, avg_sentiment, price_change_1d, status, created_at
            FROM {_SCHEMA}.mimir_social_surges
            WHERE surge_date >= %s
            ORDER BY surge_ratio DESC
        """, (since,))
        rows = cur.fetchall()
        return [
            {
                "ticker":          r[0],
                "surge_date":      r[1].isoformat() if r[1] else None,
                "baseline_volume": float(r[2]) if r[2] else None,
                "surge_volume":    int(r[3]) if r[3] else None,
                "surge_ratio":     float(r[4]) if r[4] else None,
                "avg_sentiment":   float(r[5]) if r[5] else None,
                "price_change_1d": float(r[6]) if r[6] else None,
                "status":          r[7],
                "created_at":      r[8].isoformat() if r[8] else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"[SURGE] get_active_surges error: {e}")
        return []
    finally:
        cur.close()
        if close_conn:
            conn.close()
