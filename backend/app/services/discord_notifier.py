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
from typing import Optional
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
    target_price: Optional[float],
    stop_loss: Optional[float],
    sentiment_score: Optional[float],
    catalyst_type: Optional[str],
    headline: Optional[str],
    reason: str,
    holding_period: Optional[str] = None,
    conviction_score: Optional[float] = None,
    rsi: Optional[float] = None,
) -> bool:
    """
    Posts a rich Discord embed for a new MIMIR trade alert.
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

    # Sentiment
    fields.append({
        "name": "😶 Sentiment",
        "value": _sentiment_bar(sentiment_score),
        "inline": False,
    })

    # RSI if available
    if rsi is not None:
        fields.append({
            "name": "📈 RSI (14)",
            "value": f"`{rsi:.1f}`",
            "inline": True,
        })

    # Holding period
    if holding_period:
        fields.append({
            "name": "⏳ Horizon",
            "value": holding_period,
            "inline": True,
        })

    # Headline (catalyst trigger)
    if headline:
        # Truncate to Discord field limit
        hl = headline[:500] + "…" if len(headline) > 500 else headline
        fields.append({
            "name": "📰 Catalyst Headline",
            "value": f"*{hl}*",
            "inline": False,
        })

    # Reason / thesis summary
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
        "avatar_url": "https://i.imgur.com/mFLGDKb.png",  # fallback avatar
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=8)
        if resp.status_code in (200, 204):
            logger.info(f"[DISCORD] Sent {signal_type} alert for {ticker} to Discord.")
            return True
        else:
            logger.warning(f"[DISCORD] Webhook returned {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"[DISCORD] Failed to send alert for {ticker}: {e}")
        return False
