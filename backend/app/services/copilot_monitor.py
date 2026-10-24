# backend/app/services/copilot_monitor.py
"""
MIMIR Co-Pilot IRL Exit & Ratchet Monitor
=========================================
Monitors all open real-life positions for Co-Pilot users:
1. Trailing Profit Ratchet: Alerts user to ratchet stop-loss up when price expands.
2. Stop-Loss Breach: Alerts user to exit immediately if stop is breached.
3. Profit Target: Alerts user to sell if target price is hit.
4. Holding Horizon: Alerts user when holding days expire to recycle capital.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any

from ..database import get_db_connection
from ..config import get_settings
from .copilot_allocator import record_user_sell
from .discord_notifier import send_copilot_ratchet_alert, send_copilot_exit_alert

logger = logging.getLogger(__name__)
settings = get_settings()
_SCHEMA = settings.mimir_schema


def get_live_price(ticker: str, conn=None) -> Optional[float]:
    """
    Attempts to fetch the most recent price for a ticker:
    1. Try MetaTrader 5 live tick (if connected).
    2. Try database yggdrasil.mimir_latest_prices.
    3. Fallback to yfinance with TLS session.
    """
    ticker = ticker.strip().upper()

    # 1. MT5 tick
    try:
        import MetaTrader5 as mt5
        from ..analytics.mt5_bridge import ensure_mt5_connected, resolve_mt5_symbol
        if ensure_mt5_connected():
            sym = resolve_mt5_symbol(ticker)
            if sym:
                tick = mt5.symbol_info_tick(sym)
                if tick and tick.bid > 0:
                    return float(tick.bid)
    except Exception:
        pass

    # 2. Database latest prices
    close_conn = False
    if conn is None:
        try:
            conn = get_db_connection()
            close_conn = True
        except Exception:
            conn = None

    if conn is not None:
        try:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT price FROM {_SCHEMA}.mimir_latest_prices
                    WHERE ticker = %s
                    LIMIT 1
                """, (ticker,))
                p_row = cur.fetchone()
                if p_row:
                    price_val = p_row["price"] if isinstance(p_row, dict) else p_row[0]
                    if price_val and float(price_val) > 0:
                        return float(price_val)
        except Exception:
            pass
        finally:
            if close_conn:
                conn.close()

    # 3. yfinance fallback
    try:
        import yfinance as yf
        from ..routers.prices import _get_tls_session
        session = _get_tls_session()
        tk = yf.Ticker(ticker, session=session)
        fast_info = getattr(tk, "fast_info", None)
        if fast_info and hasattr(fast_info, "last_price") and fast_info.last_price:
            return float(fast_info.last_price)
        history = tk.history(period="1d", interval="1m")
        if not history.empty and "Close" in history:
            return float(history["Close"].iloc[-1])
    except Exception as e:
        logger.warning(f"[COPILOT_MONITOR] yfinance price fallback failed for {ticker}: {e}")

    return None


def evaluate_ratchet_tiers(
    entry_price: float,
    current_price: float,
    current_sl: float
) -> Optional[Dict[str, Any]]:
    """
    Evaluates War Rig Nitroused 3-tier trailing profit ratchet:
    - Tier 1 (+2.5% gain): Move SL to break-even + 0.2%
    - Tier 2 (+5.0% gain): Move SL to lock in +2.0% profit
    - Tier 3 (+8.0% gain): Move SL to lock in +5.0% profit
    Returns ratchet dict if SL should be moved higher, else None.
    """
    if entry_price <= 0:
        return None

    gain_pct = ((current_price / entry_price) - 1.0) * 100.0
    target_sl = None
    locked_pct = 0.0
    tier_label = ""

    if gain_pct >= 8.0:
        target_sl = round(entry_price * 1.05, 2)
        locked_pct = 5.0
        tier_label = "Tier 3 (Locked +5.0% Profit)"
    elif gain_pct >= 5.0:
        target_sl = round(entry_price * 1.02, 2)
        locked_pct = 2.0
        tier_label = "Tier 2 (Locked +2.0% Profit)"
    elif gain_pct >= 2.5:
        target_sl = round(entry_price * 1.002, 2)
        locked_pct = 0.2
        tier_label = "Tier 1 (Break-Even Lock)"

    if target_sl and target_sl > current_sl:
        return {
            "tier_label": tier_label,
            "target_sl": target_sl,
            "locked_pct": locked_pct,
            "gain_pct": gain_pct
        }
    return None


def check_and_update_copilot_positions(conn=None) -> Dict[str, Any]:
    """
    Scans all active open positions in mimir_user_positions:
    1. Checks if trailing ratchet should be raised.
    2. Checks if stop-loss was breached.
    3. Checks if target price was reached.
    4. Checks if holding period has expired.
    Dispatches personalized Discord alerts for any triggered actions.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    cur = conn.cursor()
    ratchets_triggered = 0
    exits_triggered = 0
    details = []

    try:
        cur.execute(f"""
            SELECT p.id, p.user_id, p.ticker, p.shares, p.entry_price, p.entry_time,
                   p.current_stop_loss, p.target_price, p.current_ratchet_level,
                   p.holding_period_days, p.sector,
                   u.username, u.discord_id, u.discord_webhook_trades
            FROM {_SCHEMA}.mimir_user_positions p
            JOIN {_SCHEMA}.mimir_users u ON p.user_id = u.id
            WHERE p.status = 'OPEN' AND u.is_copilot_active = TRUE
        """)
        positions = cur.fetchall()

        now_utc = datetime.now(timezone.utc)

        for pos in positions:
            if isinstance(pos, dict):
                p_id = pos["id"]
                u_id = pos["user_id"]
                ticker = pos["ticker"]
                shares = float(pos["shares"])
                entry_price = float(pos["entry_price"])
                entry_time = pos["entry_time"]
                curr_sl = float(pos["current_stop_loss"])
                target_price = float(pos["target_price"]) if pos["target_price"] else None
                holding_days = int(pos["holding_period_days"] or 7)
                user_info = {
                    "id": u_id,
                    "username": pos["username"],
                    "discord_id": pos["discord_id"],
                    "discord_webhook_trades": pos["discord_webhook_trades"]
                }
            else:
                p_id, u_id, ticker, shares, entry_price, entry_time, curr_sl, target_price, _, holding_days, _, username, disc_id, webhook = (
                    pos[0], pos[1], pos[2], float(pos[3]), float(pos[4]), pos[5],
                    float(pos[6]), float(pos[7]) if pos[7] else None, pos[8],
                    int(pos[9] or 7), pos[10], pos[11], pos[12], pos[13]
                )
                user_info = {
                    "id": u_id,
                    "username": username,
                    "discord_id": disc_id,
                    "discord_webhook_trades": webhook
                }

            current_price = get_live_price(ticker, conn=conn)
            if not current_price or current_price <= 0:
                continue

            gain_pct = ((current_price / entry_price) - 1.0) * 100.0

            # ── 1. Check Stop Loss Breach ──────────────────────────────────
            if current_price <= curr_sl:
                sell_res = record_user_sell(p_id, current_price, close_reason="STOP_LOSS", conn=conn)
                if sell_res.get("success"):
                    exits_triggered += 1
                    send_copilot_exit_alert(
                        user=user_info,
                        ticker=ticker,
                        shares=shares,
                        entry_price=entry_price,
                        exit_price=current_price,
                        realized_pnl=sell_res["realized_pnl"],
                        return_pct=sell_res["return_pct"],
                        cash_recovered=sell_res["cash_recovered"],
                        new_dry_powder=sell_res["new_dry_powder"],
                        exit_reason="STOP_LOSS"
                    )
                    details.append(f"EXIT (STOP_LOSS): {ticker} for {user_info['username']} @ ${current_price:.2f}")
                    continue

            # ── 2. Check Target Price Hit ──────────────────────────────────
            if target_price and current_price >= target_price:
                sell_res = record_user_sell(p_id, current_price, close_reason="PROFIT_TARGET", conn=conn)
                if sell_res.get("success"):
                    exits_triggered += 1
                    send_copilot_exit_alert(
                        user=user_info,
                        ticker=ticker,
                        shares=shares,
                        entry_price=entry_price,
                        exit_price=current_price,
                        realized_pnl=sell_res["realized_pnl"],
                        return_pct=sell_res["return_pct"],
                        cash_recovered=sell_res["cash_recovered"],
                        new_dry_powder=sell_res["new_dry_powder"],
                        exit_reason="PROFIT_TARGET"
                    )
                    details.append(f"EXIT (PROFIT_TARGET): {ticker} for {user_info['username']} @ ${current_price:.2f}")
                    continue

            # ── 3. Check Holding Period Maturity ───────────────────────────
            if entry_time:
                # Ensure entry_time is timezone aware
                if entry_time.tzinfo is None:
                    entry_time = entry_time.replace(tzinfo=timezone.utc)
                if entry_time + timedelta(days=holding_days) <= now_utc:
                    sell_res = record_user_sell(p_id, current_price, close_reason="HOLD_MATURITY", conn=conn)
                    if sell_res.get("success"):
                        exits_triggered += 1
                        send_copilot_exit_alert(
                            user=user_info,
                            ticker=ticker,
                            shares=shares,
                            entry_price=entry_price,
                            exit_price=current_price,
                            realized_pnl=sell_res["realized_pnl"],
                            return_pct=sell_res["return_pct"],
                            cash_recovered=sell_res["cash_recovered"],
                            new_dry_powder=sell_res["new_dry_powder"],
                            exit_reason="HOLD_MATURITY"
                        )
                        details.append(f"EXIT (HOLD_MATURITY): {ticker} for {user_info['username']} ({holding_days} days)")
                        continue

            # ── 4. Evaluate Trailing Ratchet ───────────────────────────────
            ratchet_eval = evaluate_ratchet_tiers(entry_price, current_price, curr_sl)
            if ratchet_eval:
                target_sl = ratchet_eval["target_sl"]
                locked_profit = round((target_sl - entry_price) * shares, 2)

                cur.execute(f"""
                    UPDATE {_SCHEMA}.mimir_user_positions
                    SET current_stop_loss = %s,
                        current_ratchet_level = %s
                    WHERE id = %s
                """, (target_sl, target_sl, p_id))
                conn.commit()

                ratchets_triggered += 1
                send_copilot_ratchet_alert(
                    user=user_info,
                    ticker=ticker,
                    shares=shares,
                    old_sl=curr_sl,
                    new_sl=target_sl,
                    entry_price=entry_price,
                    current_price=current_price,
                    locked_profit=locked_profit,
                    gain_pct=gain_pct
                )
                details.append(f"RATCHET: {ticker} for {user_info['username']} SL moved to ${target_sl:.2f} (+${locked_profit:.2f})")

        return {
            "status": "success",
            "ratchets_triggered": ratchets_triggered,
            "exits_triggered": exits_triggered,
            "details": details
        }
    finally:
        cur.close()
        if close_conn:
            conn.close()
