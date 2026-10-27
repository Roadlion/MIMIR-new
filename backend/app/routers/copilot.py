# backend/app/routers/copilot.py
"""
MIMIR Co-Pilot API Router
==========================
REST endpoints for managing multi-user IRL portfolios, risk parameters,
logging manual/automated buy/sell executions, and checking portfolio states.
"""

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from datetime import datetime

from ..database import get_db_connection_dict, get_db_connection
from ..config import get_settings
from ..auth import get_optional_current_user
from ..services.copilot_allocator import (
    get_copilot_users,
    get_user_portfolio_state,
    evaluate_signal_for_user,
    record_user_buy,
    record_user_sell
)
from ..services.copilot_monitor import check_and_update_copilot_positions

router = APIRouter()
settings = get_settings()
_SCHEMA = settings.mimir_schema


def _resolve_user_id(request: Request, explicit_id: Optional[int] = None) -> int:
    if explicit_id:
        return explicit_id
    try:
        user = get_optional_current_user(request)
        if user and user.get("id"):
            return user["id"]
    except Exception:
        pass
    return 1  # Default to admin user 1


class MyConfigUpdate(BaseModel):
    user_id: Optional[int] = None
    discord_id: Optional[str] = None
    discord_webhook_trades: Optional[str] = None
    total_budget: Optional[float] = None
    dry_powder: Optional[float] = None
    risk_per_trade_pct: Optional[float] = None
    max_position_pct: Optional[float] = None
    max_sector_pct: Optional[float] = None
    is_copilot_active: Optional[bool] = None


@router.get("/copilot/me")
def get_my_copilot_config(request: Request):
    """Fetches Co-Pilot configuration and real-life portfolio state for the active user."""
    user_id = _resolve_user_id(request)
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT id, username, discord_id, discord_webhook_trades,
                       total_budget, dry_powder, risk_per_trade_pct,
                       max_position_pct, max_sector_pct, is_copilot_active
                FROM {_SCHEMA}.mimir_users
                WHERE id = %s
            """, (user_id,))
            u = cur.fetchone()
            if not u:
                raise HTTPException(status_code=404, detail="User not found")

            state = get_user_portfolio_state(user_id, conn=conn)
            holdings_val = float(state.get("invested_value") or 0.0)
            dry_powder = float(u[5] or 0.0)
            total_cap = round(holdings_val + dry_powder, 2)

            u_dict = {
                "id": u[0],
                "username": u[1],
                "discord_id": u[2] or "",
                "discord_webhook_trades": u[3] or "",
                "total_budget": total_cap,
                "dry_powder": dry_powder,
                "holdings_value": holdings_val,
                "holdings_count": len(state.get("open_positions", [])),
                "risk_per_trade_pct": float(u[6] or 1.0),
                "max_position_pct": float(u[7] or 10.0),
                "max_sector_pct": float(u[8] or 25.0),
                "is_copilot_active": bool(u[9]),
                "portfolio_summary": state
            }
            return {"status": "success", "data": u_dict}


@router.post("/copilot/me")
def update_my_copilot_config(payload: MyConfigUpdate, request: Request):
    """Updates Co-Pilot configuration directly from the Web UI."""
    user_id = _resolve_user_id(request, payload.user_id)
    with get_db_connection() as conn:
        # 1. Fetch current live holdings value from actual logged portfolio
        state = get_user_portfolio_state(user_id, conn=conn)
        holdings_val = float(state.get("invested_value") or 0.0)

        with conn.cursor() as cur:
            updates = []
            values = []
            if payload.discord_id is not None:
                updates.append("discord_id = %s")
                values.append(payload.discord_id.strip())
            if payload.discord_webhook_trades is not None:
                updates.append("discord_webhook_trades = %s")
                values.append(payload.discord_webhook_trades.strip())
            
            # If dry_powder is updated, total_budget is automatically (holdings + dry_powder)
            if payload.dry_powder is not None:
                updates.append("dry_powder = %s")
                values.append(payload.dry_powder)
                computed_budget = round(holdings_val + payload.dry_powder, 2)
                updates.append("total_budget = %s")
                values.append(computed_budget)
            elif payload.total_budget is not None:
                updates.append("total_budget = %s")
                values.append(payload.total_budget)

            if payload.risk_per_trade_pct is not None:
                updates.append("risk_per_trade_pct = %s")
                values.append(payload.risk_per_trade_pct)
            if payload.max_position_pct is not None:
                updates.append("max_position_pct = %s")
                values.append(payload.max_position_pct)
            if payload.max_sector_pct is not None:
                updates.append("max_sector_pct = %s")
                values.append(payload.max_sector_pct)
            if payload.is_copilot_active is not None:
                updates.append("is_copilot_active = %s")
                values.append(payload.is_copilot_active)

            if not updates:
                return {"status": "noop", "message": "No updates provided"}

            values.append(user_id)
            cur.execute(f"""
                UPDATE {_SCHEMA}.mimir_users
                SET {', '.join(updates)}
                WHERE id = %s
            """, tuple(values))
            conn.commit()

    return {"status": "success", "message": "Co-Pilot budget updated successfully", "user_id": user_id}



class UserConfigUpdate(BaseModel):
    user_id: int
    discord_id: Optional[str] = None
    discord_webhook_trades: Optional[str] = None
    total_budget: Optional[float] = None
    dry_powder: Optional[float] = None
    risk_per_trade_pct: Optional[float] = None
    max_position_pct: Optional[float] = None
    max_sector_pct: Optional[float] = None
    is_copilot_active: Optional[bool] = None


class BuyOrderRequest(BaseModel):
    user_id: int
    ticker: str
    shares: float
    entry_price: float
    stop_loss: float
    target_price: Optional[float] = None
    holding_period_days: int = 7
    sector: Optional[str] = None


class SellOrderRequest(BaseModel):
    position_id: int
    exit_price: float
    close_reason: str = "MANUAL"


@router.get("/copilot/users")
def list_copilot_users():
    """Lists all active Co-Pilot users and their current portfolio states."""
    try:
        users = get_copilot_users()
        results = []
        for u in users:
            state = get_user_portfolio_state(u["id"])
            u_copy = dict(u)
            u_copy["portfolio_summary"] = state
            results.append(u_copy)
        return {"status": "success", "count": len(results), "users": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch copilot users: {e}")


@router.post("/copilot/users/config")
def update_user_config(payload: UserConfigUpdate):
    """Updates user budget, dry powder, risk parameters, and Discord settings."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                updates = []
                values = []
                if payload.discord_id is not None:
                    updates.append("discord_id = %s")
                    values.append(payload.discord_id)
                if payload.discord_webhook_trades is not None:
                    updates.append("discord_webhook_trades = %s")
                    values.append(payload.discord_webhook_trades)
                if payload.total_budget is not None:
                    updates.append("total_budget = %s")
                    values.append(payload.total_budget)
                if payload.dry_powder is not None:
                    updates.append("dry_powder = %s")
                    values.append(payload.dry_powder)
                if payload.risk_per_trade_pct is not None:
                    updates.append("risk_per_trade_pct = %s")
                    values.append(payload.risk_per_trade_pct)
                if payload.max_position_pct is not None:
                    updates.append("max_position_pct = %s")
                    values.append(payload.max_position_pct)
                if payload.max_sector_pct is not None:
                    updates.append("max_sector_pct = %s")
                    values.append(payload.max_sector_pct)
                if payload.is_copilot_active is not None:
                    updates.append("is_copilot_active = %s")
                    values.append(payload.is_copilot_active)

                if not updates:
                    return {"status": "noop", "message": "No updates provided"}

                values.append(payload.user_id)
                cur.execute(f"""
                    UPDATE {_SCHEMA}.mimir_users
                    SET {', '.join(updates)}
                    WHERE id = %s
                """, tuple(values))
                conn.commit()

        return {"status": "success", "user_id": payload.user_id, "updated": updates}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update user config: {e}")


@router.get("/copilot/users/{user_id}/portfolio")
def get_user_portfolio(user_id: int):
    """Fetches real-time portfolio status, active holdings, and sector allocations."""
    state = get_user_portfolio_state(user_id)
    if "error" in state:
        raise HTTPException(status_code=404, detail=state["error"])
    return {"status": "success", "data": state}


@router.post("/copilot/positions/buy")
def execute_buy(payload: BuyOrderRequest):
    """Records an executed BUY order in the user's IRL portfolio and deducts dry powder."""
    res = record_user_buy(
        user_id=payload.user_id,
        ticker=payload.ticker,
        shares=payload.shares,
        entry_price=payload.entry_price,
        stop_loss=payload.stop_loss,
        target_price=payload.target_price,
        holding_period_days=payload.holding_period_days,
        sector=payload.sector
    )
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "Buy failed"))
    return res


@router.post("/copilot/positions/sell")
def execute_sell(payload: SellOrderRequest):
    """Closes an IRL position, calculates realized P&L, and restores dry powder."""
    res = record_user_sell(
        position_id=payload.position_id,
        exit_price=payload.exit_price,
        close_reason=payload.close_reason
    )
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "Sell failed"))
    return res


@router.post("/copilot/monitor/cycle")
def trigger_monitor_cycle():
    """Manually triggers an IRL exit & trailing ratchet check cycle."""
    try:
        res = check_and_update_copilot_positions()
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Monitor cycle failed: {e}")
