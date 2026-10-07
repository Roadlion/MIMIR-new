# backend/app/services/discord_notifier.py
"""
MIMIR Discord Notifier
======================
Sends rich embed trade alert cards to a Discord webhook whenever a new
signal is generated — from either the Catalyst Engine or the Signal
Fusion (XGBoost) pipeline.

Setup:
  1. Create a Discord server (or use an existing one).
  2. Go to a channel → Edit Channel → Integrations → Webhooks → New Webhook.
  3. Copy the webhook URL and add it to .env:
       DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
  4. Optionally set DISCORD_MENTION_ROLE_ID to a role ID to ping on Tier 1 alerts.
"""

import requests
import logging
from typing import Optional, List, Dict, Any
from ..config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Colour palette for Discord embed sidebars ──────────────────────────────
_COLORS = {
    "BUY":  0x00C896,   # teal-green
    "SELL": 0xFF4C5B,   # red
}

# Catalyst type → human-readable label
_CATALYST_LABELS = {
    "WAR_RIG_CONVERGENCE":    "⚔️ War Rig Alpha Convergence",
    "PRE_EARNINGS_BEAT":      "📅 Pre-Earnings Beat",
    "SUPPLY_CHAIN_SPILLOVER": "🔗 Supply Chain Spillover",
    "MICRO_CATALYST":         "⚡ Micro Catalyst",
    "THEMATIC_TREND":         "🌊 Thematic Trend",
    "MACRO_CATALYST":         "🌐 Macro Catalyst",
    "SENTIMENT_FUSION":       "🤖 Sentiment Fusion",
}


def _sentiment_bar(score: float) -> str:
    """Returns a visual emoji bar representing sentiment strength."""
    abs_score = abs(score)
    filled = round(abs_score * 5)          # 0–5 blocks
    empty  = 5 - filled
    block  = "🟩" if score >= 0 else "🟥"
    return block * filled + "⬜" * empty + f"  `{score:+.2f}`"


def _conviction_tier(conviction_score: Optional[float], catalyst_type: Optional[str], sentiment: float) -> str:
    """Assigns a human-readable conviction tier label."""
    if conviction_score is None:
        conviction_score = abs(sentiment)

    if catalyst_type == "WAR_RIG_CONVERGENCE" or (conviction_score and conviction_score >= 0.75):
        pct = round(conviction_score * 100) if conviction_score <= 1.0 else round(conviction_score)
        return f"🏆 **Tier 1 — War Rig Institutional ({pct}%)**"

    is_catalyst_driven = catalyst_type and catalyst_type != "SENTIMENT_FUSION"

    if is_catalyst_driven and conviction_score >= 0.45:
        return "🏆 **Tier 1 — High Conviction**"
    elif is_catalyst_driven or conviction_score >= 0.35:
        return "🥈 **Tier 2 — Moderate**"
    else:
        return "🥉 **Tier 3 — Watch**"


def send_trade_alert(
    ticker: str,
    signal_type: str,                       # "BUY" or "SELL"
    trigger_price: float,
    target_price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    sentiment_score: Optional[float] = None,
    catalyst_type: Optional[str] = None,
    headline: Optional[str] = None,
    reason: str = "",
    holding_period: Optional[str] = None,
    conviction_score: Optional[float] = None,
    rsi: Optional[float] = None,
    investment_thesis: Optional[str] = None,
    cylinder_details: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Posts a rich Discord embed for a new MIMIR trade alert.
    Displays detailed breakdown of points and elements across all 3 War Rig cylinders.
    Returns True if the message was sent successfully, False otherwise.
    Silently skips if DISCORD_WEBHOOK_URL is not configured.
    """
    webhook_url = getattr(settings, "discord_webhook_url", "")
    if not webhook_url:
        return False  # Not configured — skip silently

    sentiment_score = sentiment_score or 0.0
    cat_label = _CATALYST_LABELS.get(catalyst_type or "SENTIMENT_FUSION", "📊 Signal")
    conviction_label = _conviction_tier(conviction_score, catalyst_type, sentiment_score)
    color = _COLORS.get(signal_type, 0x7289DA)

    # ── Header line ──────────────────────────────────────────────────────────
    action_emoji = "🟢" if signal_type == "BUY" else "🔴"
    title = f"{action_emoji} **{signal_type}** — `${ticker}`"

    # ── Build fields ─────────────────────────────────────────────────────────
    fields = []

    # Catalyst / signal source
    fields.append({
        "name": "📡 Signal Source",
        "value": cat_label,
        "inline": True,
    })

    # Conviction
    fields.append({
        "name": "🎯 Conviction",
        "value": conviction_label,
        "inline": True,
    })

    # Spacer (Discord embeds look better with even columns)
    fields.append({"name": "\u200b", "value": "\u200b", "inline": True})

    # Price levels
    fields.append({
        "name": "💵 Entry Price",
        "value": f"`${trigger_price:,.2f}`",
        "inline": True,
    })

    if target_price:
        upside_pct = ((target_price / trigger_price) - 1) * 100
        fields.append({
            "name": "🎯 Target",
            "value": f"`${target_price:,.2f}` *(+{upside_pct:.1f}%)*",
            "inline": True,
        })

    if stop_loss:
        downside_pct = ((stop_loss / trigger_price) - 1) * 100
        fields.append({
            "name": "🛑 Stop Loss",
            "value": f"`${stop_loss:,.2f}` *({downside_pct:.1f}%)*",
            "inline": True,
        })

    # Sentiment & RSI
    fields.append({
        "name": "😶 Sentiment",
        "value": _sentiment_bar(sentiment_score),
        "inline": True,
    })

    if rsi is not None:
        fields.append({
            "name": "📈 RSI (14)",
            "value": f"`{rsi:.1f}`",
            "inline": True,
        })
    else:
        fields.append({"name": "\u200b", "value": "\u200b", "inline": True})

    # Holding period
    if holding_period:
        fields.append({
            "name": "⏳ Horizon",
            "value": holding_period,
            "inline": True,
        })

    # Headline (catalyst trigger)
    if headline:
        hl = headline[:500] + "…" if len(headline) > 500 else headline
        fields.append({
            "name": "📰 Catalyst Headline",
            "value": f"*{hl}*",
            "inline": False,
        })

    # ── War Rig Point Breakdown by Cylinder ──────────────────────────────────
    if cylinder_details:
        c1 = cylinder_details.get("c1", {})
        c2 = cylinder_details.get("c2", {})
        c3 = cylinder_details.get("c3", {})
        c1_s = cylinder_details.get("c1_score", 0.0)
        c2_s = cylinder_details.get("c2_score", 0.0)
        c3_s = cylinder_details.get("c3_score", 0.0)

        # Cylinder 1 Elements
        c1_lines = [
            f"**Points Awarded:** `{c1_s:.1f} / 25.0 pts`",
            f"• **Sector:** {c1.get('sector_name', 'General Market')} (`{c1.get('sector_phase', 'NEUTRAL')}`)",
            f"• **Relative Strength:** `{c1.get('rs_vs_spy_5d', 0.0):+.1f}%` vs SPY",
            f"• **Thesis:** {c1.get('sector_thesis', 'Sector alignment')}",
        ]
        if c1.get("macro_status") and c1.get("macro_status") != "CLEAR":
            c1_lines.append(f"• **Macro Filter:** {c1.get('macro_status')} ({c1.get('macro_reason')})")

        fields.append({
            "name": f"⚡ Cylinder 1: Macro & Sector Inflows (`{c1_s:.1f} pts`)",
            "value": "\n".join(c1_lines)[:1024],
            "inline": False,
        })

        # Cylinder 2 Elements
        c2_lines = [
            f"**Points Awarded:** `{c2_s:.1f} / 50.0 pts`",
        ]
        if c2.get("turbo_a_active"):
            pe = c2.get("pre_earnings", {})
            eps_txt = f"+{pe['eps_growth']*100:.0f}%" if pe.get("eps_growth") is not None else "N/A"
            c2_lines.append(
                f"• **Turbo A (Pre-Earnings):** `{c2.get('turbo_a_score', 0):.1f} pts`\n"
                f"  └ Report in `{pe.get('days_until', '?')}d` ({pe.get('earnings_date')}) | EPS Growth: `{eps_txt}` | Sentiment: `{pe.get('avg_sentiment', 0.0):+.2f}`"
            )
        else:
            c2_lines.append("• **Turbo A (Pre-Earnings):** `0.0 pts` (No earnings scheduled in 2–14d window)")

        if c2.get("turbo_b_active"):
            sp = c2.get("spillover", {})
            if sp:
                c2_lines.append(
                    f"• **Turbo B (Spillover):** `{c2.get('turbo_b_score', 0):.1f} pts`\n"
                    f"  └ Supplier/Peer: `{sp.get('source_asset')}` (Sentiment: `+{sp.get('spillover_score', 0):.2f}`, Conf: `{sp.get('spillover_confidence', 0):.2f}`)"
                )
            elif c2.get("headlines"):
                c2_lines.append(
                    f"• **Turbo B (High-Impact News):** `{c2.get('turbo_b_score', 0):.1f} pts`\n"
                    f"  └ *{c2['headlines'][0][:100]}*"
                )
        else:
            c2_lines.append("• **Turbo B (News/Spillover):** `0.0 pts` (No high-magnitude catalyst found)")

        if c2.get("turbo_c_active"):
            bf = c2.get("breakout_flow", {})
            c2_lines.append(
                f"• **Turbo C (Institutional Breakout Flow):** `{c2.get('turbo_c_score', 0):.1f} pts`\n"
                f"  └ Volume Surge: `{bf.get('volume_ratio', 1.0):.2f}x` | 5D Return: `+{bf.get('rs_5d', 0.0):.1f}%` | Level: `${bf.get('breakout_level', 0):.2f}`"
            )

        if c2.get("twin_turbo_synergy"):
            c2_lines.append(f"• **Multi-Chamber Synergy:** `+{c2.get('synergy_bonus', 15.0):.1f} pts` (Catalyst Confluence Bonus)")

        fields.append({
            "name": f"🔥 Cylinder 2: Catalyst V8 Multi-Chamber (`{c2_s:.1f} pts`)",
            "value": "\n".join(c2_lines)[:1024],
            "inline": False,
        })

        rsi_val = c3.get('rsi', 50)
        c3_lines = [
            f"**Points Awarded:** `{c3_s:.1f} / 28.0 pts`",
            f"• **Moving Averages:** {c3.get('trend_note', 'Bullish structure')}",
            f"• **RSI Sweet Spot:** {c3.get('rsi_note', f'RSI: {rsi_val:.1f}')}",
            f"• **Volume Expansion:** {c3.get('vol_note', 'Normal turnover')}",
        ]
        quant_parts = []
        if c3.get("vol_regime") in ("SQUEEZE", "EXHAUSTION", "EXPANDING"):
            quant_parts.append(f"Regime: `{c3.get('vol_regime')}`")
        if c3.get("seller_exhausted"):
            quant_parts.append("`Wyckoff Seller Absorption`")
        if c3.get("ou_zscore") is not None and abs(c3.get("ou_zscore")) >= 0.5:
            quant_parts.append(f"OU Z: `{c3.get('ou_zscore'):.2f}`")
        if quant_parts:
            c3_lines.append(f"• **Bong Strats Quant:** {' | '.join(quant_parts)}")

        up_pct = c3.get("upside_pct", 0)
        down_pct = c3.get("downside_pct", 0)
        rr_val = c3.get("rr_ratio", 2.5)
        c3_lines.append(f"• **Asymmetry Gate:** Target `+{up_pct}%` vs Stop `-{down_pct}%` | **R/R: `{rr_val:.2f}:1`** (Min 2.5:1 required)")

        fields.append({
            "name": f"🎯 Cylinder 3: Microstructure & Asymmetry (`{c3_s:.1f} pts`)",
            "value": "\n".join(c3_lines)[:1024],
            "inline": False,
        })

    elif investment_thesis and "CYLINDER 1" in investment_thesis:
        # Structured breakdown parsed from existing stored thesis
        lines = [ln.strip() for ln in investment_thesis.split("\n") if ln.strip()]
        c1_val = ""
        c2_val = ""
        c3_val = ""
        asym_val = ""
        for line in lines:
            if "CYLINDER 1" in line:
                c1_val = line.replace("⚡ CYLINDER 1 (Macro & Sector):", "").strip()
            elif "CYLINDER 2" in line:
                c2_val = line.replace("🔥 CYLINDER 2 (Catalyst V8):", "").strip()
            elif "CYLINDER 3" in line:
                c3_val = line.replace("🎯 CYLINDER 3 (Microstructure):", "").strip()
            elif "ASYMMETRY GATE" in line:
                asym_val = line.replace("⚖️ ASYMMETRY GATE:", "").strip()

        if c1_val:
            fields.append({"name": "⚡ Cylinder 1: Macro & Sector", "value": f"```{c1_val[:1000]}```", "inline": False})
        if c2_val:
            fields.append({"name": "🔥 Cylinder 2: Catalyst V8", "value": f"```{c2_val[:1000]}```", "inline": False})
        if c3_val or asym_val:
            c3_combined = f"{c3_val}\n\n⚖️ Asymmetry: {asym_val}" if asym_val else c3_val
            fields.append({"name": "🎯 Cylinder 3: Microstructure & Asymmetry", "value": f"```{c3_combined[:1000]}```", "inline": False})
    else:
        # Fallback summary
        reason_short = reason[:800] + "…" if len(reason) > 800 else reason
        fields.append({
            "name": "🧠 Signal Reason",
            "value": f"```{reason_short}```",
            "inline": False,
        })

    # ── Assemble payload ──────────────────────────────────────────────────────
    embed = {
        "title": title,
        "color": color,
        "fields": fields,
        "footer": {
            "text": "MIMIR — Sentiment-Powered Equity Research",
        },
        "timestamp": None,  # Discord uses ISO8601; we let it auto-timestamp via the webhook
    }

    # Optional role mention for Tier 1 signals
    content = ""
    mention_role_id = getattr(settings, "discord_mention_role_id", "")
    if mention_role_id and "Tier 1" in conviction_label:
        content = f"<@&{mention_role_id}> 🚨 High-conviction alert!"

    payload = {
        "content": content,
        "embeds": [embed],
        "username": "MIMIR Oracle",
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=8, verify=False)
        if resp.status_code in (200, 204):
            logger.info(f"[DISCORD] Sent {signal_type} alert for {ticker} to Discord.")
            return True
        else:
            logger.warning(f"[DISCORD] Webhook returned {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"[DISCORD] Failed to send alert for {ticker}: {e}")
        return False


def send_sitrep_notification(sitrep_data: dict) -> bool:
    """
    Posts a multi-embed Situation Report (Sit Rep) to Discord.
    Summarizes macro posture, bond yields (e.g. 30Y Treasury yield lows),
    multi-day market narratives, and breaking global events.
    """
    webhook_url = getattr(settings, "discord_webhook_url", "")
    if not webhook_url:
        return False

    title = f"🌐 **MIMIR GLOBAL MARKET SITUATION REPORT (SIT REP)**"
    headline_summary = sitrep_data.get("headline_summary", "Market posture summary.")
    milestones = sitrep_data.get("milestones", [])
    macro_snap = sitrep_data.get("macro_snapshot", {})
    active_narratives = sitrep_data.get("active_narratives", [])
    breaking_events = sitrep_data.get("breaking_events", [])

    embeds = []

    # Embed 1: Sit Rep Summary & Yield Milestones
    fields1 = []
    fields1.append({
        "name": "📋 Executive Summary",
        "value": f"*{headline_summary}*",
        "inline": False
    })

    if milestones:
        milestone_text = "\n".join([f"• {m}" for m in milestones])
        fields1.append({
            "name": "🚨 Yield & Macro Milestones",
            "value": milestone_text,
            "inline": False
        })

    # Benchmark posture summary table
    if macro_snap:
        snap_lines = []
        for sym, d in macro_snap.items():
            if sym in ("^TYX", "^TNX", "^VIX", "SPY", "QQQ", "GC=F", "CL=F"):
                chg = f"`{d['change_5d_pct']:+.1f}% 5d`"
                tag = " 🚨 **[52W LOW]**" if d.get("is_at_low") else " 🚨 **[52W HIGH]**" if d.get("is_at_high") else ""
                snap_lines.append(f"• **{d['name']}** (`{sym}`): `{d['current']}` ({chg}){tag}")
        if snap_lines:
            fields1.append({
                "name": "📊 Benchmark & Yield Posture",
                "value": "\n".join(snap_lines[:6]),
                "inline": False
            })

    embeds.append({
        "title": title,
        "color": 0x1E88E5,  # Professional Blue
        "fields": fields1,
        "footer": {"text": "MIMIR — Global Situation Report"},
    })

    # Embed 2: Multi-Day Narratives & Breaking Events
    fields2 = []

    if active_narratives:
        nar_lines = []
        for nar in active_narratives[:3]:
            nar_lines.append(f"• `[{nar['phase']}]` **{nar['theme']}**: Avg Sent `{nar['avg_sentiment']:+.2f}`, 3D Price `{nar.get('price_change_3d', 0.0):+.1f}%`")
        fields2.append({
            "name": "🌊 Active Multi-Day Narratives",
            "value": "\n".join(nar_lines),
            "inline": False
        })

    if breaking_events:
        ev_lines = []
        for ev in breaking_events[:3]:
            emoji = "🟢" if ev["sentiment_score"] > 0 else "🔴" if ev["sentiment_score"] < 0 else "⚪"
            ev_lines.append(f"{emoji} **{ev['title'][:80]}** *({ev['source']})*")
        fields2.append({
            "name": "📰 Breaking Global Events",
            "value": "\n".join(ev_lines),
            "inline": False
        })

    if fields2:
        embeds.append({
            "title": "🌊 **NARRATIVE & EVENT MATRIX**",
            "color": 0x673AB7,  # Deep Purple
            "fields": fields2,
        })

    payload = {
        "content": "📢 **NEW MIMIR SITUATION REPORT AVAILABLE**",
        "embeds": embeds,
        "username": "MIMIR Sit Rep",
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=8, verify=False)
        if resp.status_code in (200, 204):
            logger.info("[DISCORD] Posted Sit Rep to Discord successfully.")
            return True
        else:
            logger.warning(f"[DISCORD] Sit Rep webhook returned {resp.status_code}")
            return False
    except Exception as e:
        logger.error(f"[DISCORD] Failed to send Sit Rep notification: {e}")
        return False


def send_global_breaking_alert(
    event_category: str,
    headline: str,
    summary: str,
    source: str,
    sentiment_score: float,
    affected_assets: Optional[List[str]] = None
) -> bool:
    """
    Posts an instant Breaking Global Alert embed for major market-moving events
    (wars, emergency press releases, central bank decisions, macro yield shocks).
    """
    webhook_url = getattr(settings, "discord_webhook_url", "")
    if not webhook_url:
        return False

    color = 0xFF4C5B if sentiment_score < 0 else 0x00C896 if sentiment_score > 0 else 0xFF9800

    fields = [
        {
            "name": "📰 Headline",
            "value": f"**{headline}**",
            "inline": False
        },
        {
            "name": "📡 Event Category",
            "value": f"`{event_category}` | Source: `{source}`",
            "inline": True
        },
        {
            "name": "⚖️ Market Impact Score",
            "value": f"`{sentiment_score:+.2f}`",
            "inline": True
        }
    ]

    if summary:
        fields.append({
            "name": "📝 Context Summary",
            "value": summary[:600],
            "inline": False
        })

    if affected_assets:
        fields.append({
            "name": "🎯 Key Assets Tracked",
            "value": ", ".join([f"`{a}`" for a in affected_assets[:6]]),
            "inline": False
        })

    embed = {
        "title": f"🚨 **BREAKING GLOBAL EVENT / MACRO ALERT**",
        "color": color,
        "fields": fields,
        "footer": {"text": "MIMIR Global Intelligence Feed"},
    }

    payload = {
        "content": "🚨 @everyone **MARKET-MOVING GLOBAL EVENT DETECTED**" if abs(sentiment_score) >= 0.75 else "🚨 **GLOBAL EVENT ALERT**",
        "embeds": [embed],
        "username": "MIMIR Global Sentinel",
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=8, verify=False)
        return resp.status_code in (200, 204)
    except Exception as e:
        logger.error(f"[DISCORD] Failed to post global breaking alert: {e}")
        return False


# In-memory alert cache to prevent duplicate Discord spam for the same pair within 2 hours
_RECENT_STAT_ARB_ALERTS: Dict[str, datetime] = {}


def _zscore_visual_gauge(z: float) -> str:
    """Generates a visual gauge bar showing spread dislocation relative to entry & stop bands."""
    # Scale Z from -3.5 to +3.5 across 13 segments
    clamped_z = max(-3.5, min(3.5, z))
    norm_idx = int(round((clamped_z + 3.5) / 7.0 * 12))
    norm_idx = max(0, min(12, norm_idx))
    bar = ["="] * 13
    bar[6] = "|"  # Mean
    bar[2] = "["  # -2 sigma Long Entry
    bar[10] = "]" # +2 sigma Short Entry
    bar[norm_idx] = "o"
    gauge_str = "".join(bar)
    direction = "OVERSOLD (Long Spread)" if z < 0 else "OVERBOUGHT (Short Spread)"
    return f"`-3.5s [{gauge_str}] +3.5s`\n**Z-Score:** `{z:+.2f}s` ({direction})"



def send_stat_arb_alert(
    ticker1: str,
    ticker2: str,
    pair_name: str,
    cluster: str,
    action: str,                # e.g. "LONG NVDA, SHORT AMD"
    z_score: float,
    hedge_ratio: float,
    half_life_bars: float,
    adf_t_stat: float,
    correlation: float,
    sentiment_t1: float,
    sentiment_t2: float,
    sentiment_delta: float,
    conviction: str,
    rationale: str,
    price_t1: Optional[float] = None,
    price_t2: Optional[float] = None,
    historical_win_rate: Optional[float] = None,
    historical_profit_factor: Optional[float] = None,
) -> bool:
    """
    Sends a dedicated, market-neutral Stat-Arb APU Discord embed alert.
    Distinct from directional War Rig breakout alerts:
      - Formatted specifically for dual-leg pairs execution.
      - Displays dynamic Kalman beta hedge ratio, spread Z-score gauge, ADF stationarity,
        Ornstein-Uhlenbeck half-life, and DeepSeek sentiment circuit-breaker verdict.
      - Deduplicates alerts per pair to prevent notification spam.
    """
    from datetime import datetime, timezone, timedelta

    webhook_url = getattr(settings, "discord_webhook_url", "")
    if not webhook_url:
        return False

    pair_key = f"{ticker1}:{ticker2}"
    now_utc = datetime.now(timezone.utc)

    # 1. Anti-Spam Check: don't alert the same pair more than once every 2 hours
    last_sent = _RECENT_STAT_ARB_ALERTS.get(pair_key)
    if last_sent and (now_utc - last_sent) < timedelta(hours=2):
        logger.info(f"[DISCORD] Stat-Arb alert for {pair_key} suppressed (sent {fmtTime(last_sent)} ago).")
        return False

    # Color: Gold for High Conviction, Cyan for Confirmed, Purple for Medium
    if "HIGH" in conviction:
        embed_color = 0xFFD700  # Gold
    elif "CONFIRMED" in conviction:
        embed_color = 0x00A6B2  # Cyan/Teal
    elif "VETOED" in conviction:
        embed_color = 0xFF4C5B  # Red
    else:
        embed_color = 0x8B5CF6  # Violet

    # Parse primary and hedge leg actions
    is_long_t1 = "LONG " + ticker1 in action
    leg1_act = "🟢 BUY" if is_long_t1 else "🔴 SELL"
    leg2_act = "🔴 SELL" if is_long_t1 else "🟢 BUY"

    p1_str = f" @ `${price_t1:.2f}`" if price_t1 else ""
    p2_str = f" @ `${price_t2:.2f}`" if price_t2 else ""

    fields = [
        # 1. Action & Pair Cluster
        {
            "name": "⚖️ Dual-Leg Pair Orders (MT5 Magic #202608)",
            "value": (
                f"• **Leg 1 (Primary):** {leg1_act} `1.00 Lot` **{ticker1}**{p1_str}\n"
                f"• **Leg 2 (Hedge):** {leg2_act} `{hedge_ratio:.2f} Lots` **{ticker2}**{p2_str} *(β = {hedge_ratio:.3f})*\n"
                f"• **Cluster:** `{cluster}` | `{pair_name}`"
            ),
            "inline": False
        },
        # 2. Spread Z-Score & Statistical Geometry
        {
            "name": "📊 Spread Deviation & Mean Reversion",
            "value": _zscore_visual_gauge(z_score),
            "inline": True
        },
        {
            "name": "📐 Cointegration Quality",
            "value": (
                f"• **Stationarity:** `ADF t = {adf_t_stat:.2f}` *(p < 0.05)*\n"
                f"• **Correlation:** `r = {correlation:+.3f}`\n"
                f"• **Half-Life (&tau;):** `~{half_life_bars:.1f} bars`"
            ),
            "inline": True
        },
        # 3. Sentiment Circuit-Breaker Verdict
        {
            "name": "🤖 DeepSeek Sentiment Circuit-Breaker",
            "value": (
                f"• **{ticker1} Sentiment:** `{sentiment_t1:+.2f}`\n"
                f"• **{ticker2} Sentiment:** `{sentiment_t2:+.2f}`\n"
                f"• **Sentiment &Delta; (T1 - T2):** `{sentiment_delta:+.2f}`\n"
                f"• **Verdict:** **{conviction}**\n"
                f"• **Rationale:** *{rationale}*"
            ),
            "inline": False
        },
        # 4. Trade Management & Exit Plan
        {
            "name": "🎯 Target & Exit Parameters",
            "value": (
                f"• **Profit Target:** Reversion to `Z = 0.0σ` *(Partial TP at 0.5σ)*\n"
                f"• **Disaster Stop:** Spread divergence at `|Z| >= 3.50σ`\n"
                f"• **Time-Out Exit:** `2.5 × Half-Life` *(approx. {round(half_life_bars * 2.5)} bars)*"
            ),
            "inline": True
        },
    ]

    # 5. Historical Track Record
    if historical_win_rate:
        pf_str = f" | PF: `{historical_profit_factor:.2f}x`" if historical_profit_factor else ""
        fields.append({
            "name": "🏆 Cluster Track Record",
            "value": (
                f"• **Backtested Win Rate:** `{historical_win_rate * 100:.1f}%`{pf_str}\n"
                f"• **Market Exposure:** `Zero-Beta Market Neutral`"
            ),
            "inline": True
        })

    title = f"⚖️ **STAT-ARB APU PAIR ALERT** &mdash; `{ticker1}` vs `{ticker2}`"

    embed = {
        "title": title,
        "color": embed_color,
        "fields": fields,
        "footer": {
            "text": "MIMIR Stat-Arb APU — Market-Neutral Quantitative Arbitrage",
        },
    }

    # Role mention for high conviction pair trades
    content = ""
    mention_role_id = getattr(settings, "discord_mention_role_id", "")
    if mention_role_id and "HIGH" in conviction:
        content = f"<@&{mention_role_id}> 🚨 High-Conviction Stat-Arb Pair Opportunity!"

    payload = {
        "content": content,
        "embeds": [embed],
        "username": "MIMIR Stat-Arb APU",
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=8, verify=False)
        if resp.status_code in (200, 204):
            _RECENT_STAT_ARB_ALERTS[pair_key] = now_utc
            logger.info(f"[DISCORD] Sent Stat-Arb APU alert for {pair_key} to Discord.")
            return True
        else:
            logger.warning(f"[DISCORD] Stat-Arb webhook returned {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"[DISCORD] Failed to send Stat-Arb alert for {pair_key}: {e}")
        return False


