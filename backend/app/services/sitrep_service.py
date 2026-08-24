# backend/app/services/sitrep_service.py
"""
MIMIR Situation Report (Sit Rep) & Macro Milestone Service
==========================================================
Compiles comprehensive Situation Reports (Sit Reps) summarizing:
1. Macro Posture & Bond Yield Milestones (e.g. 30Y Treasury Yield ^TYX at low/high, 10Y ^TNX, VIX, Oil, Gold).
2. Global Breaking Events & Geopolitics (wars, central bank emergency moves, major press releases).
3. Multi-Day Narrative Matrix (emerging/building multi-day market themes combining sentiment + 3d/5d price returns).
4. Key Market Risks & Central Bank Regimes.

Persists compiled Sit Reps to `mimir_sitreps` table and triggers notifications.
"""

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any
import yfinance as yf
from yfinance import cache as yf_cache

try:
    yf_cache.get_cookie_cache().dummy = True
except Exception:
    pass

from ..database import get_db_connection
from ..config import get_settings
from ..services.macro_tracker import get_macro_regime_summary
from ..analytics.narrative_tracker import get_all_active_narratives

logger = logging.getLogger(__name__)
settings = get_settings()
_SCHEMA = settings.mimir_schema

# Benchmark macro tickers
MACRO_BENCHMARKS = {
    "^TYX": "US 30Y Treasury Yield",
    "^TNX": "US 10Y Treasury Yield",
    "^FVX": "US 5Y Treasury Yield",
    "^VIX": "CBOE Volatility Index",
    "SPY":  "S&P 500 ETF",
    "QQQ":  "Nasdaq 100 ETF",
    "GC=F": "Gold Futures",
    "CL=F": "Crude Oil Futures",
    "DX-Y.NYB": "US Dollar Index",
    "BTC-USD": "Bitcoin",
}


def _ensure_sitrep_table(conn):
    """Ensures mimir_sitreps table exists."""
    cur = conn.cursor()
    try:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {_SCHEMA}.mimir_sitreps (
                id              SERIAL PRIMARY KEY,
                created_at      TIMESTAMPTZ DEFAULT NOW(),
                report_type     TEXT DEFAULT 'PERIODIC', -- 'PERIODIC', 'ON_DEMAND', 'EVENT_TRIGGERED'
                title           TEXT NOT NULL,
                headline_summary TEXT NOT NULL,
                macro_snapshot  JSONB,
                active_narratives JSONB,
                breaking_events JSONB,
                full_markdown   TEXT NOT NULL
            );
        """)
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.warning(f"[SITREP] Ensure table error: {e}")
    finally:
        cur.close()


import threading
from curl_cffi.requests import Session

_tls = threading.local()

def _get_tls_session():
    if not hasattr(_tls, 'session'):
        sess = Session(impersonate="chrome")
        sess.verify = False
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9"
        })
        _tls.session = sess
    return _tls.session


def fetch_macro_snapshot() -> Dict[str, Any]:
    """
    Fetches real-time / daily price history for macro benchmarks and computes:
    - Current price/yield
    - 1-day % change
    - 5-day % change
    - 52-week High / Low proximity (detecting 30Y yield lows, VIX spikes, etc.)
    """
    snapshot = {}
    tickers = list(MACRO_BENCHMARKS.keys())
    session = _get_tls_session()
    
    for symbol in tickers:
        name = MACRO_BENCHMARKS[symbol]
        try:
            t = yf.Ticker(symbol, session=session)
            hist = t.history(period="1y", interval="1d")
            if hist.empty or len(hist) < 2:
                continue

            last_close = float(hist["Close"].iloc[-1])
            prev_close = float(hist["Close"].iloc[-2])
            pct_change_1d = ((last_close - prev_close) / prev_close) * 100.0 if prev_close else 0.0

            five_day_close = float(hist["Close"].iloc[-5]) if len(hist) >= 5 else prev_close
            pct_change_5d = ((last_close - five_day_close) / five_day_close) * 100.0 if five_day_close else 0.0

            high_52w = float(hist["High"].max())
            low_52w = float(hist["Low"].min())

            # Range position: 0.0 = at 52w low, 1.0 = at 52w high
            range_span = high_52w - low_52w
            range_pct = ((last_close - low_52w) / range_span) if range_span > 0 else 0.5

            is_at_low = range_pct <= 0.10
            is_at_high = range_pct >= 0.90

            snapshot[symbol] = {
                "symbol": symbol,
                "name": name,
                "current": round(last_close, 3 if "Yield" in name or symbol in ("^TYX", "^TNX", "^FVX") else 2),
                "change_1d_pct": round(pct_change_1d, 2),
                "change_5d_pct": round(pct_change_5d, 2),
                "high_52w": round(high_52w, 3),
                "low_52w": round(low_52w, 3),
                "range_pct": round(range_pct * 100, 1),
                "is_at_low": is_at_low,
                "is_at_high": is_at_high,
            }
        except Exception as e:
            logger.warning(f"[SITREP] Error fetching benchmark {symbol}: {e}")

    return snapshot


def fetch_breaking_global_events(limit: int = 5, conn=None) -> List[Dict[str, Any]]:
    """
    Fetches recent high-impact global news articles (wars, central banks, macro shocks, press releases)
    from the last 48 hours.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    events = []
    cur = conn.cursor()
    try:
        # Fetch articles scored with high sentiment magnitude or macro keywords
        cur.execute(f"""
            SELECT a.id, a.title, a.summary, a.source_name, a.published_ts,
                   si.asset_name, si.sentiment_score, si.magnitude, si.policy_signal, si.reasoning
            FROM {_SCHEMA}.mimir_raw_articles a
            JOIN {_SCHEMA}.mimir_sentiment_impacts si ON si.article_id = a.id
            WHERE a.published_ts >= NOW() - INTERVAL '48 hours'
              AND (
                  ABS(si.sentiment_score) >= 0.5
                  OR si.policy_signal IS NOT NULL
                  OR a.title ILIKE '%%war%%' OR a.title ILIKE '%%tariff%%' OR a.title ILIKE '%%fed%%'
                  OR a.title ILIKE '%%yield%%' OR a.title ILIKE '%%sanction%%' OR a.title ILIKE '%%crisis%%'
                  OR a.title ILIKE '%%inflation%%' OR a.title ILIKE '%%cpi%%'
              )
            ORDER BY a.published_ts DESC
            LIMIT %s
        """, (limit * 2,))
        rows = cur.fetchall()

        seen_titles = set()
        for r in rows:
            art_id, title, summary, source, pub_ts, asset, score, mag, policy, reasoning = r
            clean_title = title.strip()
            if clean_title.lower() in seen_titles:
                continue
            seen_titles.add(clean_title.lower())

            events.append({
                "article_id": art_id,
                "title": clean_title,
                "source": source,
                "published_at": pub_ts.isoformat() if pub_ts else None,
                "asset": asset,
                "sentiment_score": float(score) if score is not None else 0.0,
                "magnitude": mag,
                "policy_signal": policy,
                "summary": summary[:250] if summary else "",
            })
            if len(events) >= limit:
                break
    except Exception as e:
        logger.error(f"[SITREP] Error fetching breaking events: {e}")
    finally:
        cur.close()
        if close_conn:
            conn.close()

    return events


def compile_sitrep(report_type: str = "PERIODIC", conn=None) -> Dict[str, Any]:
    """
    Compiles a complete Situation Report (Sit Rep).
    Returns dict with report details and formatted Markdown text.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    _ensure_sitrep_table(conn)

    now = datetime.now(timezone.utc)
    timestamp_str = now.strftime("%Y-%m-%d %H:%M UTC")

    # 1. Macro Snapshot & Milestones
    macro_snap = fetch_macro_snapshot()

    # Highlight key milestone observations (e.g. 30Y yield low)
    milestones = []
    if "^TYX" in macro_snap:
        tyx = macro_snap["^TYX"]
        if tyx["is_at_low"]:
            milestones.append(f"📉 **30-Year Treasury Yield (`^TYX`) at 52-Week Low** ({tyx['current']:.3f}%, 5d: {tyx['change_5d_pct']:+.2f}%)")
        elif tyx["is_at_high"]:
            milestones.append(f"📈 **30-Year Treasury Yield (`^TYX`) at 52-Week High** ({tyx['current']:.3f}%, 5d: {tyx['change_5d_pct']:+.2f}%)")

    if "^TNX" in macro_snap:
        tnx = macro_snap["^TNX"]
        if tnx["is_at_low"]:
            milestones.append(f"📉 **10-Year Treasury Yield (`^TNX`) at 52-Week Low** ({tnx['current']:.3f}%)")
        elif tnx["is_at_high"]:
            milestones.append(f"📈 **10-Year Treasury Yield (`^TNX`) at 52-Week High** ({tnx['current']:.3f}%)")

    if "^VIX" in macro_snap:
        vix = macro_snap["^VIX"]
        if vix["current"] >= 25.0:
            milestones.append(f"⚠️ **VIX Spike**: `{vix['current']:.2f}` (1d: {vix['change_1d_pct']:+.1f}%) — High Volatility Regime")
        elif vix["current"] <= 14.0:
            milestones.append(f"🟢 **VIX Low**: `{vix['current']:.2f}` — Low Volatility / Risk-On")

    # 2. Central Bank Macro Regime
    cb_regimes = get_macro_regime_summary(conn=conn)

    # 3. Active Multi-Day Narratives
    active_narratives = get_all_active_narratives(conn=conn)

    # 4. Breaking Global News & Events
    breaking_events = fetch_breaking_global_events(limit=5, conn=conn)

    # 5. Formulate Headline Summary
    top_headline_parts = []
    if milestones:
        top_headline_parts.append(milestones[0])
    if active_narratives:
        top_narrative = active_narratives[0]
        top_headline_parts.append(f"Dominant Narrative: **{top_narrative['theme']}** ({top_narrative['phase']})")
    elif breaking_events:
        top_headline_parts.append(f"Top Breaking: {breaking_events[0]['title'][:60]}...")

    headline_summary = " | ".join(top_headline_parts) if top_headline_parts else "Market steady across benchmarks."

    # 6. Build Full Markdown Document
    md = []
    md.append(f"# 🌐 MIMIR GLOBAL MARKET SITUATION REPORT (SIT REP)")
    md.append(f"**Generated**: {timestamp_str} | **Type**: `{report_type}`\n")

    md.append(f"### 📋 Executive Summary")
    md.append(f"{headline_summary}\n")

    if milestones:
        md.append(f"### 🚨 Key Macro Milestones & Yield Extremes")
        for m in milestones:
            md.append(f"- {m}")
        md.append("")

    # Benchmark Table
    md.append(f"### 📊 Benchmark Posture & Yield Snapshot")
    md.append("| Asset / Benchmark | Symbol | Price / Yield | 1D Change | 5D Change | 52W Range Pos |")
    md.append("| :--- | :--- | :---: | :---: | :---: | :---: |")
    for sym, data in macro_snap.items():
        change_1d = f"`{data['change_1d_pct']:+.2f}%`"
        change_5d = f"`{data['change_5d_pct']:+.2f}%`"
        range_str = f"{data['range_pct']:.0f}% {'(LOW 🚨)' if data['is_at_low'] else '(HIGH 🚨)' if data['is_at_high'] else ''}"
        md.append(f"| {data['name']} | `{data['symbol']}` | `{data['current']}` | {change_1d} | {change_5d} | {range_str} |")
    md.append("")

    # Central Bank Macro Regimes
    if cb_regimes:
        md.append(f"### 🏛️ Central Bank Policy Regimes")
        for cb, details in cb_regimes.items():
            sig = details.get("policy_signal", "neutral").upper()
            badge = "🔴 HAWKISH" if sig == "HAWKISH" else "🟢 DOVISH" if sig == "DOVISH" else "⚪ NEUTRAL"
            md.append(f"- **{cb}**: {badge} *(Headline: \"{details.get('headline', '')[:80]}\")*")
        md.append("")

    # Multi-Day Narrative Matrix
    md.append(f"### 🌊 Multi-Day Market Narrative Matrix")
    if active_narratives:
        for nar in active_narratives[:4]:
            phase_badge = f"`[{nar['phase']}]`"
            sent_str = f"Avg Sentiment: `{nar['avg_sentiment']:+.2f}`"
            ret_str = f"3D Price Return: `{nar.get('price_change_3d', 0.0):+.2f}%`"
            md.append(f"- {phase_badge} **{nar['theme']}** ({nar['category']})")
            md.append(f"  └ *{sent_str}* | *{ret_str}* | Catalysts: {nar['article_count']} articles")
            md.append(f"  └ Summary: _{nar['summary']}_")
    else:
        md.append("No active multi-day narrative anomalies detected in current window.\n")
    md.append("")

    # Breaking Global Events
    md.append(f"### 📰 Breaking Global Events & Market Catalysts")
    if breaking_events:
        for ev in breaking_events:
            score_emoji = "🟢" if ev["sentiment_score"] > 0 else "🔴" if ev["sentiment_score"] < 0 else "⚪"
            md.append(f"- {score_emoji} **{ev['title']}** ({ev['source']})")
            if ev.get("asset"):
                md.append(f"  └ Asset Impact: `{ev['asset']}` | Sentiment: `{ev['sentiment_score']:+.2f}`")
    else:
        md.append("No breaking high-impact macro shocks in past 48h.\n")

    full_markdown = "\n".join(md)

    # 7. Persist into DB
    cur = conn.cursor()
    try:
        import json
        cur.execute(f"""
            INSERT INTO {_SCHEMA}.mimir_sitreps
                (report_type, title, headline_summary, macro_snapshot, active_narratives, breaking_events, full_markdown)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            report_type,
            f"MIMIR Sit Rep — {timestamp_str}",
            headline_summary,
            json.dumps(macro_snap),
            json.dumps(active_narratives),
            json.dumps(breaking_events),
            full_markdown
        ))
        row = cur.fetchone()
        sitrep_id = row[0] if row else None
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"[SITREP] Error saving Sit Rep to DB: {e}")
        sitrep_id = None
    finally:
        cur.close()

    result = {
        "id": sitrep_id,
        "created_at": timestamp_str,
        "report_type": report_type,
        "title": f"MIMIR Sit Rep — {timestamp_str}",
        "headline_summary": headline_summary,
        "milestones": milestones,
        "macro_snapshot": macro_snap,
        "cb_regimes": cb_regimes,
        "active_narratives": active_narratives,
        "breaking_events": breaking_events,
        "full_markdown": full_markdown,
    }

    # 8. Dispatch notification via Discord
    try:
        from ..services.discord_notifier import send_sitrep_notification
        send_sitrep_notification(result)
    except Exception as discord_err:
        logger.warning(f"[SITREP] Discord notification failed: {discord_err}")

    if close_conn:
        conn.close()

    return result


def get_latest_sitrep(conn=None) -> Optional[Dict[str, Any]]:
    """Fetches the most recent Sit Rep from database."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    _ensure_sitrep_table(conn)

    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT id, created_at, report_type, title, headline_summary,
                   macro_snapshot, active_narratives, breaking_events, full_markdown
            FROM {_SCHEMA}.mimir_sitreps
            ORDER BY created_at DESC
            LIMIT 1
        """)
        row = cur.fetchone()
        if not row:
            return None

        return {
            "id": row[0],
            "created_at": row[1].isoformat() if row[1] else None,
            "report_type": row[2],
            "title": row[3],
            "headline_summary": row[4],
            "macro_snapshot": row[5],
            "active_narratives": row[6],
            "breaking_events": row[7],
            "full_markdown": row[8],
        }
    except Exception as e:
        logger.error(f"[SITREP] Error fetching latest sitrep: {e}")
        return None
    finally:
        cur.close()
        if close_conn:
            conn.close()
