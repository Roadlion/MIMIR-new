# backend/app/routers/paper_trading.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any

from ..analytics.paper_trader import (
    get_paper_config,
    update_paper_config,
    auto_execute_pending_alerts,
    process_paper_position_exits,
    get_paper_trading_summary,
    close_paper_position,
    reset_paper_account
)

router = APIRouter()

class PaperConfigUpdate(BaseModel):
    is_enabled: Optional[bool] = None
    execution_mode: Optional[str] = None
    min_win_rate: Optional[float] = None
    min_sentiment_score: Optional[float] = None
    position_size_type: Optional[str] = None
    position_size_value: Optional[float] = None
    initial_capital: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    auto_exit_on_hold_days: Optional[bool] = None

class ClosePositionPayload(BaseModel):
    ticker: str

@router.get("/config")
def api_get_paper_config():
    """Returns the current paper trading configuration settings."""
    try:
        return get_paper_config()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading paper config: {str(e)}")

@router.post("/config")
def api_update_paper_config(payload: PaperConfigUpdate):
    """Updates paper trading settings."""
    try:
        updates = payload.dict(exclude_unset=True)
        return update_paper_config(updates)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating paper config: {str(e)}")

@router.get("/summary")
def api_get_paper_summary():
    """Returns the paper trading portfolio summary, equity curve metrics, active positions, and trade logs."""
    try:
        return get_paper_trading_summary()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating paper summary: {str(e)}")

@router.post("/auto-trade")
def api_trigger_auto_trade():
    """Triggers an immediate auto-execution pass over pending trade alerts."""
    try:
        res = auto_execute_pending_alerts()
        # Also run exit checks
        exits = process_paper_position_exits()
        res["position_exits"] = exits
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Auto-trade trigger error: {str(e)}")

@router.post("/close-position")
def api_close_paper_position(payload: ClosePositionPayload):
    """Manually closes an active paper trading position."""
    try:
        res = close_paper_position(payload.ticker)
        if not res.get("success", True):
            raise HTTPException(status_code=400, detail=res.get("message"))
        return res
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Close position error: {str(e)}")

@router.post("/reset")
def api_reset_paper_account():
    """Resets paper trading portfolio back to starting virtual capital."""
    try:
        return reset_paper_account()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Reset paper account error: {str(e)}")
