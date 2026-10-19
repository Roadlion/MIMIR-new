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


_RECENT_MACRO_SHOCKS = {}


def evaluate_macro_shocks(conn=None) -> List[Dict[str, Any]]:
    """
    Evaluates real-time macro shocks across benchmarks, VIX, bond yields, and Fed stance.
    Dispatches instant high-priority Discord alert embeds when anomalies occur,
    using an anti-spam cooldown cache (4 hours per shock type).
    """
    shocks_detected = []
    now_utc = datetime.now(timezone.utc)

    try:
        snap = fetch_macro_snapshot()
        if not snap:
            return []

        # 1. VIX Volatility Shock
        if "^VIX" in snap:
            vix = snap["^VIX"]
            val = float(vix.get("current", 0.0))
            chg1d = float(vix.get("change_1d_pct", 0.0))

            vix_key = None
            severity = "HIGH"
            title = ""
            desc = ""

            if val >= 30.0:
                vix_key = "VIX_EXTREME_PANIC"
                severity = "CRITICAL"
                title = f"VIX Extreme Volatility Spike ({val:.2f})"
                desc = f"CBOE VIX has breached the extreme 30.0 panic threshold ({val:.2f}, {chg1d:+.1f}% today). High risk liquidation cascade possible."
            elif val >= 25.0:
                vix_key = "VIX_ELEVATED_RISK"
                severity = "HIGH"
                title = f"VIX Elevated Risk Threshold Breached ({val:.2f})"
                desc = f"CBOE VIX is trading at {val:.2f} (> 25.0). Long swing positions are defensively restricted by macro risk gates."
            elif chg1d >= 12.0:
                vix_key = "VIX_RAPID_SPIKE"
                severity = "HIGH"
                title = f"VIX Rapid Intraday Surge ({chg1d:+.1f}%)"
                desc = f"VIX jumped {chg1d:+.1f}% in today's session to {val:.2f}. Option hedging activity is spiking."

            if vix_key:
                last_time = _RECENT_MACRO_SHOCKS.get(vix_key)
                if not last_time or (now_utc - last_time) >= timedelta(hours=4):
                    try:
                        from .discord_notifier import send_macro_shock_alert
                        sent = send_macro_shock_alert(
                            shock_type=vix_key,
                            title=title,
                            headline=desc,
                            metrics={
                                "CBOE VIX": f"{val:.2f}",
                                "1-Day Change": f"{chg1d:+.2f}%",
                                "52-Week Range": f"{vix.get('range_pct', 0.0):.1f}%"
                            },
                            severity=severity
                        )
                        if sent:
                            _RECENT_MACRO_SHOCKS[vix_key] = now_utc
                            shocks_detected.append({"type": vix_key, "title": title})
                    except Exception as err:
                        logger.warning(f"[SITREP] VIX shock alert error: {err}")

        # 2. Treasury Yield Spikes / 52W Extremes
        for y_sym in ("^TNX", "^TYX"):
            if y_sym in snap:
                yd = snap[y_sym]
                y_val = float(yd.get("current", 0.0))
                y_chg = float(yd.get("change_1d_pct", 0.0))
                is_hi = yd.get("is_at_high", False)
                is_lo = yd.get("is_at_low", False)

                y_key = None
                y_title = ""
                y_desc = ""

                if is_hi:
                    y_key = f"{y_sym}_52W_HIGH"
                    y_title = f"{yd['name']} at 52-Week High ({y_val:.3f}%)"
                    y_desc = f"{yd['name']} ({y_sym}) reached 52-week high ({y_val:.3f}%). Multiple compression risk on high-multiple equities."
                elif is_lo:
                    y_key = f"{y_sym}_52W_LOW"
                    y_title = f"{yd['name']} at 52-Week Low ({y_val:.3f}%)"
                    y_desc = f"{yd['name']} ({y_sym}) reached 52-week low ({y_val:.3f}%). Flight-to-safety or disinflation trade dominant."
                elif y_chg >= 4.0:
                    y_key = f"{y_sym}_INTRA_SURGE"
                    y_title = f"{yd['name']} Intraday Yield Surge ({y_chg:+.1f}%)"
                    y_desc = f"Rapid rate repricing: {yd['name']} rose {y_chg:+.1f}% in a single session to {y_val:.3f}%."

                if y_key:
                    last_time = _RECENT_MACRO_SHOCKS.get(y_key)
                    if not last_time or (now_utc - last_time) >= timedelta(hours=6):
                        try:
                            from .discord_notifier import send_macro_shock_alert
                            sent = send_macro_shock_alert(
                                shock_type=y_key,
                                title=y_title,
                                headline=y_desc,
                                metrics={
                                    "Benchmark": yd["name"],
                                    "Current Yield": f"{y_val:.3f}%",
                                    "1-Day Change": f"{y_chg:+.2f}%",
                                    "52W High / Low": f"{yd.get('high_52w')}% / {yd.get('low_52w')}%"
                                },
                                severity="HIGH"
                            )
                            if sent:
                                _RECENT_MACRO_SHOCKS[y_key] = now_utc
                                shocks_detected.append({"type": y_key, "title": y_title})
                        except Exception as err:
                            logger.warning(f"[SITREP] Yield shock alert error: {err}")

        # 3. Market Trend / Regime Flip Check (SPY vs 50-day SMA)
        close_conn = False
        if conn is None:
            conn = get_db_connection()
            close_conn = True

        try:
            from .macro_tracker import is_market_regime_bullish, get_smoothed_central_bank_signal
            is_bull, regime_reason = is_market_regime_bullish(conn=conn)
            if not is_bull:
                r_key = "REGIME_DEFENSIVE_FLIP"
                last_time = _RECENT_MACRO_SHOCKS.get(r_key)
                if not last_time or (now_utc - last_time) >= timedelta(hours=6):
                    from .discord_notifier import send_macro_shock_alert
                    sent = send_macro_shock_alert(
                        shock_type=r_key,
                        title="Market Regime Flipped to DEFENSIVE",
                        headline=regime_reason,
                        metrics={"Regime Status": "DEFENSIVE", "Action": "Long Swing Entries Suppressed"},
                        severity="HIGH"
                    )
                    if sent:
                        _RECENT_MACRO_SHOCKS[r_key] = now_utc
                        shocks_detected.append({"type": r_key, "title": "Regime Defensive"})

            # 4. Central Bank Stance Flip Check
            fed_stance, fed_score = get_smoothed_central_bank_signal("Federal Reserve", lookback_hours=48, conn=conn)
            if fed_stance in ("hawkish", "dovish") and abs(fed_score) >= 0.35:
                fed_key = f"FED_STANCE_{fed_stance.upper()}"
                last_time = _RECENT_MACRO_SHOCKS.get(fed_key)
                if not last_time or (now_utc - last_time) >= timedelta(hours=12):
                    from .discord_notifier import send_macro_shock_alert
                    sent = send_macro_shock_alert(
                        shock_type=fed_key,
                        title=f"Federal Reserve Macro Regime: {fed_stance.upper()}",
                        headline=f"48-hour EMA smoothed Fed policy score reached {fed_score:+.2f} ({fed_stance.upper()} territory).",
                        metrics={
                            "Fed Smoothed Score": f"{fed_score:+.2f}",
                            "Policy Stance": fed_stance.upper(),
                            "Rate-Sensitive Sectors": "BLOCKED" if fed_stance == "hawkish" else "UNRESTRICTED"
                        },
                        severity="HIGH"
                    )
                    if sent:
                        _RECENT_MACRO_SHOCKS[fed_key] = now_utc
                        shocks_detected.append({"type": fed_key, "title": f"Fed {fed_stance}"})
        finally:
            if close_conn and conn:
                conn.close()

    except Exception as e:
        logger.error(f"[SITREP] Error in evaluate_macro_shocks: {e}")

    return shocks_detected


def compile_session_briefing(session_type: str = "PRE_MARKET", conn=None) -> Dict[str, Any]:
    """
    Compiles and dispatches a scheduled Pre-Market Briefing (09:00 ET) or Post-Market Debrief (16:30 ET).
    """
    is_pre = (session_type.upper() == "PRE_MARKET")
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    try:
        cur = conn.cursor()
        macro_snap = fetch_macro_snapshot()

        # 1. Fetch War Rig Focus Candidates (recently generated / pending)
        cur.execute(f"""
            SELECT ticker, signal_type, conviction_score, sentiment_score, headline, reason, trigger_price
            FROM {_SCHEMA}.mimir_trade_signals
            WHERE catalyst_type = 'WAR_RIG_CONVERGENCE'
              AND (status = 'PENDING' OR created_at >= NOW() - INTERVAL '24 hours')
            ORDER BY conviction_score DESC, created_at DESC
            LIMIT 4
        """)
        focus_rows = cur.fetchall()
        focus_tickers = [
            {
                "ticker": r[0],
                "signal_type": r[1],
                "conviction_score": float(r[2] or 0.5),
                "sentiment_score": float(r[3] or 0.0),
                "headline": r[4] or "",
                "reason": r[5] or "",
                "trigger_price": float(r[6] or 0.0)
            }
            for r in focus_rows
        ]

        # 2. Fetch Stretched Stat-Arb Pairs (Z-score >= 1.5)
        stat_arb_pairs = []
        try:
            cur.execute(f"""
                SELECT ticker1, ticker2, z_score, status, current_spread, mean_spread
                FROM {_SCHEMA}.mimir_pair_signals
                WHERE status = 'OPEN' OR ABS(z_score) >= 1.5
                ORDER BY ABS(z_score) DESC
                LIMIT 4
            """)
            arb_rows = cur.fetchall()
            stat_arb_pairs = [
                {
                    "ticker1": r[0],
                    "ticker2": r[1],
                    "z_score": float(r[2] or 0.0),
                    "signal": r[3] or "MONITOR",
                    "current_spread": float(r[4] or 0.0),
                    "mean_spread": float(r[5] or 0.0)
                }
                for r in arb_rows
            ]
        except Exception as arb_err:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.warning(f"[SITREP] Stat-Arb pairs fetch warning: {arb_err}")

        # 3. Portfolio Performance Stats
        portfolio_stats = {}
        try:
            from ..analytics.paper_trader import get_active_portfolio_composition
            comp = get_active_portfolio_composition(conn=conn)
            cur.execute(f"""
                SELECT COALESCE(SUM(realized_pnl), 0.0)
                FROM {_SCHEMA}.mimir_paper_trade_log
                WHERE exit_time >= CURRENT_DATE
            """)
            pnl_row = cur.fetchone()
            realized_today = float(pnl_row[0]) if pnl_row else 0.0

            portfolio_stats = {
                "total_equity": comp.get("total_equity", 100000.0),
                "active_positions": comp.get("total_open_positions", 0),
                "realized_pnl_today": realized_today
            }
        except Exception as p_err:
            logger.warning(f"[SITREP] Portfolio stats gathering error: {p_err}")

        # Headline formulate
        if is_pre:
            vix_val = macro_snap.get("^VIX", {}).get("current", 18.0)
            headline = f"Wall Street opening bell preparation | VIX at {vix_val:.1f} | {len(focus_tickers)} War Rig alpha setups queued."
            title = "🌅 MIMIR PRE-MARKET BATTLE BRIEFING"
        else:
            pnl_val = portfolio_stats.get("realized_pnl_today", 0.0)
            headline = f"Wall Street regular session closed | Realized P&L today: ${pnl_val:+,.2f} | {portfolio_stats.get('active_positions', 0)} active MT5 positions held."
            title = "🌆 MIMIR POST-MARKET DEBRIEF & SETTLEMENT"

        from .discord_notifier import send_market_session_briefing
        sent = send_market_session_briefing(
            session_type=session_type,
            title=title,
            headline=headline,
            macro_snapshot=macro_snap,
            focus_tickers=focus_tickers,
            stat_arb_pairs=stat_arb_pairs,
            portfolio_stats=portfolio_stats
        )

        return {
            "session_type": session_type,
            "success": sent,
            "headline": headline,
            "focus_count": len(focus_tickers),
            "arb_count": len(stat_arb_pairs)
        }
    except Exception as e:
        logger.error(f"[SITREP] Error compiling {session_type} briefing: {e}")
        return {"session_type": session_type, "success": False, "error": str(e)}
    finally:
        if close_conn and conn:
            conn.close()
