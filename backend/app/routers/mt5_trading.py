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
from backend.app.services.mt5_live_engine import (
    get_engine_status,
    start_live_engine,
    stop_live_engine,
    update_live_config,
)
from backend.app.analytics.mt5_risk_analyzer import (
    fetch_and_pair_mt5_deals,
    compute_risk_metrics,
    run_monte_carlo_stress_test,
    run_volatility_scenario_stress_test,
)

router = APIRouter(prefix="/api/mt5", tags=["MT5 Direct Live Trading"])


class MT5LiveConfigModel(BaseModel):
    strategy_id: Optional[str] = None
    symbol: Optional[str] = None
    timeframe: Optional[str] = None
    lot_size: Optional[float] = None
    poll_interval: Optional[float] = None
    max_open_positions: Optional[int] = None
    live_trading_enabled: Optional[bool] = None
    enable_mean_exit: Optional[bool] = None
    zscore_exit: Optional[float] = None
    enable_time_exit: Optional[bool] = None
    max_holding_minutes: Optional[float] = None


@router.get("/engine/status")
def get_live_engine_status():
    """Returns the comprehensive real-time trading engine state."""
    return get_engine_status()


@router.post("/engine/start")
def start_engine():
    """Starts the autonomous tick-by-tick real-time trading loop."""
    return start_live_engine()


@router.post("/engine/stop")
def stop_engine():
    """Stops the autonomous real-time trading loop."""
    return stop_live_engine()


@router.post("/engine/config")
def update_engine_configuration(cfg: MT5LiveConfigModel):
    """Updates live strategy parameters, symbol, timeframe, or lot size."""
    data = cfg.dict(exclude_unset=True)
    if "live_trading_enabled" in data:
        mt5_exec.LIVE_TRADING_ENABLED = bool(data["live_trading_enabled"])
    updated = update_live_config(data)
    return {
        "success": True,
        "config": updated,
        "live_trading_enabled": mt5_exec.LIVE_TRADING_ENABLED,
    }


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


@router.get("/risk/analysis")
def get_risk_analysis(
    days: int = Query(default=90, ge=1, le=3650),
    symbol: Optional[str] = Query(default=None),
    magic: Optional[int] = Query(default=None)
):
    """
    Computes institutional risk metrics & Monte Carlo simulations from MT5 trade deals.
    Asset-agnostic and strategy-agnostic.
    """
    trades = fetch_and_pair_mt5_deals(days=days, symbol=symbol, magic=magic)
    metrics = compute_risk_metrics(trades)
    monte_carlo = run_monte_carlo_stress_test(trades)
    return {
        "success": True,
        "query": {
            "days": days,
            "symbol": symbol or "ALL",
            "magic": magic or "ALL"
        },
        "metrics": metrics,
        "monte_carlo": monte_carlo
    }


@router.get("/risk/scenarios")
def get_scenario_stress_test(
    symbol: str = Query(default="XAUUSD"),
    timeframe: str = Query(default="M15"),
    strategy_id: str = Query(default="gold_math_scalper")
):
    """
    Simulates strategy resilience under parametric volatility shocks (1.0x, 1.5x, 2.0x, 3.0x).
    """
    result = run_volatility_scenario_stress_test(
        symbol=symbol,
        timeframe=timeframe,
        strategy_id=strategy_id
    )
    return {
        "success": True,
        "data": result
    }

