# backend/app/services/copilot_allocator.py
"""
MIMIR Multi-User Portfolio Co-Pilot Allocator
==============================================
Evaluates raw War Rig signals against individual user IRL portfolios:
1. Sector Concentration Gate (prevents correlated overexposure)
2. Risk-Budget Sizing (N_risk based on user's defined % risk to Stop Loss)
3. Single Position Cap (N_max_pos based on max % of equity)
4. Dry Powder Clamping (N_cash based on available unallocated cash)
"""

import math
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple
from decimal import Decimal

from ..database import get_db_connection
from ..config import get_settings
from .sector_rotation_service import get_ticker_sector_tailwinds

logger = logging.getLogger(__name__)
settings = get_settings()
_SCHEMA = settings.mimir_schema


def get_copilot_users(conn=None) -> List[Dict[str, Any]]:
    """Fetches all active co-pilot users."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT id, username, discord_id, discord_webhook_trades,
                   total_budget, dry_powder, risk_per_trade_pct,
                   max_position_pct, max_sector_pct, is_copilot_active
            FROM {_SCHEMA}.mimir_users
            WHERE is_copilot_active = TRUE
            ORDER BY id ASC
        """)
        users = cur.fetchall()
        # Convert tuple / dict format to standard dict
        result = []
        for u in users:
            if isinstance(u, dict):
                result.append(dict(u))
            else:
                result.append({
                    "id": u[0],
                    "username": u[1],
                    "discord_id": u[2],
                    "discord_webhook_trades": u[3],
                    "total_budget": float(u[4] or 0.0),
                    "dry_powder": float(u[5] or 0.0),
                    "risk_per_trade_pct": float(u[6] or 1.0),
                    "max_position_pct": float(u[7] or 10.0),
                    "max_sector_pct": float(u[8] or 25.0),
                    "is_copilot_active": bool(u[9])
                })
        return result
    finally:
        cur.close()
        if close_conn:
            conn.close()


def get_user_portfolio_state(user_id: int, conn=None) -> Dict[str, Any]:
    """
    Computes real-life portfolio composition, total equity, dry powder,
    and sector allocations for a specific user.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    cur = conn.cursor()
    try:
        # 1. Fetch user budget & dry powder
        cur.execute(f"""
            SELECT total_budget, dry_powder, risk_per_trade_pct, max_position_pct, max_sector_pct
            FROM {_SCHEMA}.mimir_users
            WHERE id = %s
        """, (user_id,))
        urow = cur.fetchone()
        if not urow:
            return {"error": "User not found"}

        if isinstance(urow, dict):
            dry_powder = float(urow["dry_powder"] or 0.0)
            risk_pct = float(urow["risk_per_trade_pct"] or 1.0)
            max_pos_pct = float(urow["max_position_pct"] or 10.0)
            max_sec_pct = float(urow["max_sector_pct"] or 25.0)
        else:
            dry_powder = float(urow[1] or 0.0)
            risk_pct = float(urow[2] or 1.0)
            max_pos_pct = float(urow[3] or 10.0)
            max_sec_pct = float(urow[4] or 25.0)

        # 2. Fetch open positions
        cur.execute(f"""
            SELECT id, ticker, shares, entry_price, current_stop_loss, target_price,
                   current_ratchet_level, holding_period_days, sector, entry_time
            FROM {_SCHEMA}.mimir_user_positions
            WHERE user_id = %s AND status = 'OPEN'
        """, (user_id,))
        pos_rows = cur.fetchall()

        invested_value = 0.0
        sector_breakdown: Dict[str, float] = {}
        positions = []

        for p in pos_rows:
            if isinstance(p, dict):
                pid = p["id"]
                ticker = p["ticker"]
                shares = float(p["shares"])
                entry_price = float(p["entry_price"])
                sl = float(p["current_stop_loss"])
                tp = float(p["target_price"]) if p["target_price"] else None
                ratchet = float(p["current_ratchet_level"]) if p["current_ratchet_level"] else None
                hold_days = int(p["holding_period_days"] or 7)
                sec = p["sector"] or "UNKNOWN"
                entry_dt = p["entry_time"]
            else:
                pid, ticker, shares, entry_price, sl, tp, ratchet, hold_days, sec, entry_dt = (
                    p[0], p[1], float(p[2]), float(p[3]), float(p[4]),
                    float(p[5]) if p[5] else None, float(p[6]) if p[6] else None,
                    int(p[7] or 7), p[8] or "UNKNOWN", p[9]
                )

            val = shares * entry_price
            invested_value += val
            sector_breakdown[sec] = sector_breakdown.get(sec, 0.0) + val

            positions.append({
                "position_id": pid,
                "ticker": ticker,
                "shares": shares,
                "entry_price": entry_price,
                "current_value": val,
                "stop_loss": sl,
                "target_price": tp,
                "current_ratchet_level": ratchet,
                "holding_period_days": hold_days,
                "sector": sec,
                "entry_time": entry_dt
            })

        total_equity = dry_powder + invested_value

        # Calculate sector percentages
        sector_weights = {}
        for s, sval in sector_breakdown.items():
            sector_weights[s] = (sval / total_equity * 100.0) if total_equity > 0 else 0.0

        return {
            "user_id": user_id,
            "dry_powder": dry_powder,
            "invested_value": invested_value,
            "total_equity": total_equity,
            "risk_per_trade_pct": risk_pct,
            "max_position_pct": max_pos_pct,
            "max_sector_pct": max_sec_pct,
            "open_positions": positions,
            "sector_breakdown": sector_breakdown,
            "sector_weights": sector_weights
        }
    finally:
        cur.close()
        if close_conn:
            conn.close()


def clean_dime_shares(raw_shares: float) -> float:
    """
    Rounds raw fractional share calculations into human-friendly, clean numbers
    specifically optimized for Dime! brokerage orders (e.g. 0.12, 0.125, 0.25, 0.50, 1.25, 5).
    Eliminates awkward floating decimals like 1.1128.
    """
    if raw_shares < 0.01:
        return 0.0

    if raw_shares < 1.0:
        # Round down to nearest 0.005 step (preserves clean 0.125, 0.25, 0.12, 0.15, etc.)
        cand = math.floor(raw_shares * 200.0) / 200.0
        # If cand has 3 decimals (like 0.125, 0.375), return 3 decimals, else 2
        return round(cand, 3) if round(cand * 100, 1) % 1 != 0 else round(cand, 2)

    elif raw_shares < 5.0:
        # Between 1 and 5 shares: round down to nearest 0.05 (e.g. 1.10, 1.25, 1.50, 2.75)
        cand = math.floor(raw_shares * 20.0) / 20.0
        return int(cand) if cand % 1 == 0 else round(cand, 2)

    elif raw_shares < 20.0:
        # Between 5 and 20 shares: round down to nearest 0.5 or whole share (e.g. 5.5, 6, 7.5, 12)
        cand = math.floor(raw_shares * 2.0) / 2.0
        return int(cand) if cand % 1 == 0 else round(cand, 1)

    else:
        # 20+ shares: clean whole integer shares
        return int(math.floor(raw_shares))


def evaluate_signal_for_user(user: Dict[str, Any], signal: Dict[str, Any], conn=None) -> Dict[str, Any]:
    """
    Evaluates whether an incoming War Rig signal is suitable for a specific user,
    and calculates exact share sizing.
    """
    user_id = user["id"]
    portfolio_state = get_user_portfolio_state(user_id, conn=conn)
    if "error" in portfolio_state:
        return {"approved": False, "shares": 0, "reason": portfolio_state["error"]}

    ticker = signal["ticker"].strip().upper()
    entry_price = float(signal.get("trigger_price") or signal.get("entry_price") or 0.0)
    if entry_price <= 0:
        return {"approved": False, "shares": 0, "reason": "INVALID_ENTRY_PRICE"}

    stop_loss = signal.get("stop_loss")
    if stop_loss is not None:
        stop_loss = float(stop_loss)
    else:
        # Default stop loss 3.0% below entry if not provided
        stop_loss = round(entry_price * 0.97, 2)

    stop_distance = abs(entry_price - stop_loss)
    if stop_distance <= 0:
        stop_distance = max(0.01, entry_price * 0.02)

    target_price = float(signal["target_price"]) if signal.get("target_price") else None
    
    # Check if user already holds this ticker
    for pos in portfolio_state["open_positions"]:
        if pos["ticker"] == ticker:
            return {
                "approved": False,
                "shares": 0,
                "reason": f"ALREADY_HELD: You already hold {pos['shares']} shares of {ticker}."
            }

    # Resolve Sector
    sector = signal.get("sector")
    if not sector:
        tw = get_ticker_sector_tailwinds(ticker, conn=conn)
        sector = tw.get("sector") or "UNKNOWN"

    total_equity = portfolio_state["total_equity"]
    dry_powder = portfolio_state["dry_powder"]
    max_sector_pct = portfolio_state["max_sector_pct"]
    max_pos_pct = portfolio_state["max_position_pct"]
    risk_pct = portfolio_state["risk_per_trade_pct"]

    # 1. Sector Concentration Guardrail
    current_sec_val = portfolio_state["sector_breakdown"].get(sector, 0.0)
    current_sec_weight = (current_sec_val / total_equity * 100.0) if total_equity > 0 else 0.0

    if current_sec_weight >= max_sector_pct:
        return {
            "approved": False,
            "shares": 0,
            "sector": sector,
            "current_sector_pct": current_sec_weight,
            "max_sector_pct": max_sector_pct,
            "reason": f"SECTOR_CAP_EXCEEDED: Sector '{sector}' is already at {current_sec_weight:.1f}% of portfolio (Cap: {max_sector_pct:.1f}%)."
        }

    # 2. Risk-Based Sizing (N_risk)
    risk_capital = total_equity * (risk_pct / 100.0)
    raw_n_risk = risk_capital / stop_distance

    # 3. Max Single Position Cap (N_max_pos)
    max_pos_capital = total_equity * (max_pos_pct / 100.0)
    raw_n_max_pos = max_pos_capital / entry_price

    # 4. Dry Powder Clamp (N_cash)
    raw_n_cash = dry_powder / entry_price

    # 5. Sector Headroom Clamp (N_sec_headroom)
    max_sec_dollars = total_equity * (max_sector_pct / 100.0)
    headroom_dollars = max(0.0, max_sec_dollars - current_sec_val)
    raw_n_sec = headroom_dollars / entry_price

    # Clean Dime! Fractional Sizing (e.g. 0.12, 0.125, 0.25, 1.25, 5, etc.)
    raw_min = min(raw_n_risk, raw_n_max_pos, raw_n_cash, raw_n_sec)
    n_final = clean_dime_shares(raw_min)

    if n_final <= 0.0:
        if raw_n_cash < 0.01 or dry_powder < 2.0:
            rejection = f"INSUFFICIENT_DRY_POWDER: Available dry powder is ${dry_powder:.2f}."
        elif raw_n_risk < 0.01:
            rejection = f"RISK_BUDGET_TOO_LOW: Max risk capital ${risk_capital:.2f} too small for stop distance ${stop_distance:.2f}."
        elif headroom_dollars < 2.0:
            rejection = f"SECTOR_HEADROOM_EXHAUSTED: Only ${headroom_dollars:.2f} headroom left in {sector}."
        else:
            rejection = f"POSITION_CAP_LIMIT: Max position cap ${max_pos_capital:.2f} too small."
        
        return {
            "approved": False,
            "shares": 0,
            "sector": sector,
            "reason": rejection,
            "dry_powder": dry_powder,
            "total_equity": total_equity
        }

    estimated_cost = round(n_final * entry_price, 2)
    risk_dollars = round(n_final * stop_distance, 2)
    dry_powder_after = round(dry_powder - estimated_cost, 2)
    new_sec_weight = round(((current_sec_val + estimated_cost) / total_equity) * 100.0, 1)

    return {
        "approved": True,
        "ticker": ticker,
        "shares": n_final,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "target_price": target_price,
        "stop_distance": round(stop_distance, 2),
        "estimated_cost": estimated_cost,
        "risk_dollars": risk_dollars,
        "risk_pct_actual": round((risk_dollars / total_equity) * 100.0, 2),
        "position_pct": round((estimated_cost / total_equity) * 100.0, 1),
        "dry_powder_before": dry_powder,
        "dry_powder_after": dry_powder_after,
        "sector": sector,
        "current_sector_pct": round(current_sec_weight, 1),
        "new_sector_pct": new_sec_weight,
        "total_equity": total_equity,
        "holding_period_days": int(signal.get("holding_period_days") or 7),
        "catalyst_type": signal.get("catalyst_type"),
        "conviction_score": signal.get("conviction_score")
    }


def record_alert_dispatched(user_id: int, signal_id: Optional[int], eval_result: Dict[str, Any], conn=None) -> int:
    """Logs the personalized alert in yggdrasil.mimir_user_trade_alerts."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    cur = conn.cursor()
    try:
        cur.execute(f"""
            INSERT INTO {_SCHEMA}.mimir_user_trade_alerts (
                user_id, signal_id, ticker, action, recommended_shares, estimated_cost,
                entry_price, stop_loss, target_price, dry_powder_before, dry_powder_after, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'SENT')
            RETURNING id
        """, (
            user_id,
            signal_id,
            eval_result["ticker"],
            "BUY",
            eval_result["shares"],
            eval_result["estimated_cost"],
            eval_result["entry_price"],
            eval_result["stop_loss"],
            eval_result["target_price"],
            eval_result["dry_powder_before"],
            eval_result["dry_powder_after"]
        ))
        row = cur.fetchone()
        alert_id = row["id"] if isinstance(row, dict) else row[0]
        conn.commit()
        return alert_id
    finally:
        cur.close()
        if close_conn:
            conn.close()


def record_user_buy(
    user_id: int,
    ticker: str,
    shares: float,
    entry_price: float,
    stop_loss: float,
    target_price: Optional[float] = None,
    holding_period_days: int = 7,
    sector: Optional[str] = None,
    conn=None
) -> Dict[str, Any]:
    """
    Executes an IRL BUY entry for a user:
    - Deducts (shares * entry_price) from dry powder.
    - Records open position in mimir_user_positions.
    - Records transaction in mimir_portfolio for tracking.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    cur = conn.cursor()
    try:
        ticker = ticker.strip().upper()
        total_cost = round(shares * entry_price, 2)

        # Check dry powder
        cur.execute(f"SELECT dry_powder, total_budget FROM {_SCHEMA}.mimir_users WHERE id = %s", (user_id,))
        urow = cur.fetchone()
        if not urow:
            return {"success": False, "error": "User not found"}

        dry_powder = float(urow["dry_powder"] if isinstance(urow, dict) else urow[0])
        if dry_powder < total_cost:
            return {
                "success": False,
                "error": f"Insufficient dry powder. Available: ${dry_powder:.2f}, Required: ${total_cost:.2f}"
            }

        # Resolve sector if None
        if not sector:
            tw = get_ticker_sector_tailwinds(ticker, conn=conn)
            sector = tw.get("sector") or "UNKNOWN"

        # 1. Deduct dry powder
        new_dry_powder = round(dry_powder - total_cost, 2)
        cur.execute(f"""
            UPDATE {_SCHEMA}.mimir_users
            SET dry_powder = %s
            WHERE id = %s
        """, (new_dry_powder, user_id))

        # 2. Insert into mimir_user_positions
        cur.execute(f"""
            INSERT INTO {_SCHEMA}.mimir_user_positions (
                user_id, ticker, shares, entry_price, entry_time, current_stop_loss,
                target_price, current_ratchet_level, holding_period_days, sector, status
            ) VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s, %s, %s, 'OPEN')
            RETURNING id
        """, (
            user_id, ticker, shares, entry_price, stop_loss, target_price,
            stop_loss, holding_period_days, sector
        ))
        pos_row = cur.fetchone()
        pos_id = pos_row["id"] if isinstance(pos_row, dict) else pos_row[0]

        # 3. Insert into mimir_portfolio for legacy dashboard sync
        cur.execute(f"""
            INSERT INTO {_SCHEMA}.mimir_portfolio (
                user_id, ticker, order_date, buy_price, quantity, transaction_type, source
            ) VALUES (%s, %s, NOW(), %s, %s, 'BUY', 'COPILOT')
        """, (user_id, ticker, entry_price, shares))

        conn.commit()
        return {
            "success": True,
            "position_id": pos_id,
            "ticker": ticker,
            "shares": shares,
            "entry_price": entry_price,
            "total_cost": total_cost,
            "remaining_dry_powder": new_dry_powder
        }
    except Exception as e:
        conn.rollback()
        logger.error(f"[COPILOT] Error recording buy for user {user_id}: {e}")
        return {"success": False, "error": str(e)}
    finally:
        cur.close()
        if close_conn:
            conn.close()


def record_user_sell(
    position_id: int,
    exit_price: float,
    close_reason: str = "MANUAL",
    conn=None
) -> Dict[str, Any]:
    """
    Closes an IRL position for a user:
    - Calculates realized P&L.
    - Restores capital (shares * exit_price) into user's dry powder.
    - Updates mimir_user_positions to status='CLOSED'.
    - Records sell transaction into mimir_portfolio.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT id, user_id, ticker, shares, entry_price, sector, status
            FROM {_SCHEMA}.mimir_user_positions
            WHERE id = %s
        """, (position_id,))
        pos = cur.fetchone()
        if not pos:
            return {"success": False, "error": "Position not found"}

        if isinstance(pos, dict):
            status = pos["status"]
            user_id = pos["user_id"]
            ticker = pos["ticker"]
            shares = float(pos["shares"])
            entry_price = float(pos["entry_price"])
        else:
            status = pos[6]
            user_id = pos[1]
            ticker = pos[2]
            shares = float(pos[3])
            entry_price = float(pos[4])

        if status != "OPEN":
            return {"success": False, "error": f"Position already {status}"}

        # Calculate P&L & recovered cash
        cash_recovered = round(shares * exit_price, 2)
        realized_pnl = round((exit_price - entry_price) * shares, 2)
        return_pct = round(((exit_price / entry_price) - 1.0) * 100.0, 2)

        # 1. Update position
        cur.execute(f"""
            UPDATE {_SCHEMA}.mimir_user_positions
            SET status = 'CLOSED',
                close_price = %s,
                close_time = NOW(),
                close_reason = %s,
                realized_pnl = %s
            WHERE id = %s
        """, (exit_price, close_reason, realized_pnl, position_id))

        # 2. Restore dry powder + pnl
        cur.execute(f"""
            UPDATE {_SCHEMA}.mimir_users
            SET dry_powder = dry_powder + %s
            WHERE id = %s
            RETURNING dry_powder
        """, (cash_recovered, user_id))
        urow = cur.fetchone()
        new_dry_powder = float(urow["dry_powder"] if isinstance(urow, dict) else urow[0])

        # 3. Insert SELL transaction into mimir_portfolio
        cur.execute(f"""
            INSERT INTO {_SCHEMA}.mimir_portfolio (
                user_id, ticker, order_date, buy_price, quantity, transaction_type, source
            ) VALUES (%s, %s, NOW(), %s, %s, 'SELL', 'COPILOT')
        """, (user_id, ticker, exit_price, shares))

        conn.commit()
        return {
            "success": True,
            "position_id": position_id,
            "ticker": ticker,
            "shares": shares,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "realized_pnl": realized_pnl,
            "return_pct": return_pct,
            "cash_recovered": cash_recovered,
            "new_dry_powder": new_dry_powder,
            "close_reason": close_reason
        }
    except Exception as e:
        conn.rollback()
        logger.error(f"[COPILOT] Error recording sell for pos {position_id}: {e}")
        return {"success": False, "error": str(e)}
    finally:
        cur.close()
        if close_conn:
            conn.close()


def dispatch_copilot_signals(signal: Dict[str, Any], signal_id: Optional[int] = None, conn=None) -> List[Dict[str, Any]]:
    """
    Broadcasts an incoming War Rig signal across all active Co-Pilot users,
    calculating individualized share sizing and sending personalized Discord alerts.
    """
    from .discord_notifier import send_copilot_buy_alert

    users = get_copilot_users(conn=conn)
    dispatched = []

    for user in users:
        try:
            eval_res = evaluate_signal_for_user(user, signal, conn=conn)
            if eval_res.get("approved"):
                # Record alert in DB
                alert_id = record_alert_dispatched(user["id"], signal_id, eval_res, conn=conn)
                eval_res["alert_id"] = alert_id
                eval_res["user_id"] = user["id"]
                eval_res["username"] = user["username"]

                # Send Discord alert
                sent = send_copilot_buy_alert(user=user, eval_result=eval_res)
                eval_res["discord_sent"] = sent
                dispatched.append(eval_res)
                logger.info(
                    f"[COPILOT] Dispatched personalized alert for {user['username']}: "
                    f"BUY {eval_res['shares']} {signal['ticker']} @ ${eval_res['entry_price']} "
                    f"(Cost: ${eval_res['estimated_cost']}, Rem. Dry Powder: ${eval_res['dry_powder_after']})"
                )
            else:
                logger.info(
                    f"[COPILOT] Skipped {signal.get('ticker')} for {user['username']}: {eval_res.get('reason')}"
                )
        except Exception as e:
            logger.error(f"[COPILOT] Error dispatching to user {user.get('id')}: {e}")

    return dispatched

