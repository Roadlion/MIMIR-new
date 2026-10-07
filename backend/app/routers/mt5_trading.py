# backend/app/routers/mt5_trading.py
"""
API Router for Direct MetaTrader 5 Live Trading Operations.
Provides endpoints for account status, active positions, manual order execution,
and live trading configuration.
"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

from backend.app.integration.mt5_executor import (
    get_mt5_account_info,
    get_mt5_positions,
    send_mt5_order,
    close_mt5_positions,
    DEFAULT_LOT_SIZE,
    DEFAULT_MAGIC_NUMBER,
    LIVE_TRADING_ENABLED,
)
import backend.app.integration.mt5_executor as mt5_exec

router = APIRouter(prefix="/api/mt5", tags=["MT5 Direct Live Trading"])


class MT5OrderRequest(BaseModel):
    ticker: str
    action: str  # OPEN_LONG, OPEN_SHORT, CLOSE_LONG, CLOSE_SHORT, BUY, SELL
    volume: float = DEFAULT_LOT_SIZE
    sl_pct: float = 0.0
    tp_pct: float = 0.0
    comment: str = "MIMIR Direct API Order"


class MT5ConfigUpdate(BaseModel):
    live_trading_enabled: Optional[bool] = None
    default_lot_size: Optional[float] = None


@router.get("/status")
def get_status():
    """Returns connection status, account summary, and live trading settings."""
    account_info = get_mt5_account_info()
    return {
        "live_trading_enabled": mt5_exec.LIVE_TRADING_ENABLED,
        "default_lot_size": mt5_exec.DEFAULT_LOT_SIZE,
        "magic_number": mt5_exec.DEFAULT_MAGIC_NUMBER,
        "account_info": account_info,
    }


@router.post("/config")
def update_config(config: MT5ConfigUpdate):
    """Updates live trading settings (enable/disable, default lot size)."""
    if config.live_trading_enabled is not None:
        mt5_exec.LIVE_TRADING_ENABLED = config.live_trading_enabled
    if config.default_lot_size is not None and config.default_lot_size > 0:
        mt5_exec.DEFAULT_LOT_SIZE = config.default_lot_size
    return {
        "message": "MT5 configuration updated successfully.",
        "live_trading_enabled": mt5_exec.LIVE_TRADING_ENABLED,
        "default_lot_size": mt5_exec.DEFAULT_LOT_SIZE,
    }


@router.get("/positions")
def list_positions(symbol: Optional[str] = None):
    """Lists open MT5 positions directly from broker terminal."""
    positions = get_mt5_positions(symbol=symbol)
    return {
        "count": len(positions),
        "positions": positions,
    }


@router.post("/order")
def execute_order(order: MT5OrderRequest):
    """Places a direct market order or position closure on MT5."""
    result = send_mt5_order(
        ticker=order.ticker,
        action=order.action,
        volume=order.volume,
        sl_pct=order.sl_pct,
        tp_pct=order.tp_pct,
        comment=order.comment,
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.post("/close/{ticker}")
def close_ticker_positions(ticker: str):
    """Closes all open MT5 positions for a specific ticker."""
    result = close_mt5_positions(ticker=ticker)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result
