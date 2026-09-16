# backend/app/routers/paper_trading.py
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel
from typing import Optional, Dict, Any, Union
from datetime import datetime, timezone, timedelta
import csv
import io
import json

from ..database import get_db_connection_dict
from ..config import get_settings
from ..analytics.paper_trader import (
    get_paper_config,
    update_paper_config,
    auto_execute_pending_alerts,
    process_paper_position_exits,
    get_paper_trading_summary,
    close_paper_position,
    edit_paper_position,
    edit_paper_signal,
    edit_paper_order_history,
    delete_paper_order_history,
    reset_paper_account
)
from ..analytics.mt5_bridge import close_all_positions

router = APIRouter()
settings = get_settings()

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
    us_stocks_only: Optional[bool] = None
    mt5_magic: Optional[int] = None
    mt5_enabled: Optional[bool] = None
    ignore_market_hours: Optional[bool] = None

class ClosePositionPayload(BaseModel):
    ticker: Optional[str] = None
    ticket: Optional[int] = None

class EditPositionPayload(BaseModel):
    ticker: Optional[str] = None
    ticket: Optional[int] = None
    quantity: Optional[float] = None
    buy_price: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None

class EditSignalPayload(BaseModel):
    signal_id: int
    trigger_price: Optional[float] = None
    signal_type: Optional[str] = None

class EditPaperOrderHistoryPayload(BaseModel):
    ticker: str
    action: str
    entry_price: float
    exit_price: Optional[float] = None
    quantity: float = 1.0
    exit_reason: Optional[str] = None
    notes: Optional[str] = None

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
    """Returns the live paper trading portfolio summary, active positions, and trade logs directly from MT5."""
    try:
        return get_paper_trading_summary()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error generating paper summary: {str(e)}")

@router.post("/auto-trade")
def api_trigger_auto_trade():
    """Triggers an immediate auto-execution pass over pending trade alerts in MT5."""
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
    """Manually closes an active MT5 paper trading position by ticket or ticker."""
    try:
        target = payload.ticket if payload.ticket is not None else payload.ticker
        if not target:
            raise HTTPException(status_code=400, detail="Must provide either ticket or ticker to close position.")
        res = close_paper_position(target)
        if not res.get("success", True):
            raise HTTPException(status_code=400, detail=res.get("message"))
        return res
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Close position error: {str(e)}")

@router.post("/close-all")
def api_close_all_paper_positions():
    """Closes all active open positions in MT5."""
    try:
        res = close_all_positions()
        if not res.get("success", True):
            raise HTTPException(status_code=400, detail=res.get("message"))
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error closing all positions: {str(e)}")

@router.post("/edit-position")
def api_edit_paper_position(payload: EditPositionPayload):
    """Edits position parameters (SL / TP in MT5, or legacy quantity/entry price)."""
    try:
        target = payload.ticket if payload.ticket is not None else payload.ticker
        if not target:
            raise HTTPException(status_code=400, detail="Must provide either ticket or ticker to edit position.")
        res = edit_paper_position(
            ticker_or_ticket=target,
            new_quantity=payload.quantity,
            new_buy_price=payload.buy_price,
            sl=payload.sl,
            tp=payload.tp
        )
        if not res.get("success", True):
            raise HTTPException(status_code=400, detail=res.get("message"))
        return res
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Edit position error: {str(e)}")

@router.post("/edit-signal")
def api_edit_paper_signal(payload: EditSignalPayload):
    """Edits trigger price and/or signal type for a pending trade signal."""
    try:
        res = edit_paper_signal(payload.signal_id, payload.trigger_price, payload.signal_type)
        if not res.get("success", True):
            raise HTTPException(status_code=400, detail=res.get("message"))
        return res
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Edit signal error: {str(e)}")

@router.put("/order-history/{log_id}")
def api_edit_paper_order_history(log_id: int, payload: EditPaperOrderHistoryPayload):
    """Edits an audit paper trade order history record in PostgreSQL."""
    try:
        res = edit_paper_order_history(
            log_id=log_id,
            ticker=payload.ticker,
            action=payload.action,
            entry_price=payload.entry_price,
            exit_price=payload.exit_price,
            quantity=payload.quantity,
            exit_reason=payload.exit_reason,
            notes=payload.notes
        )
        if not res.get("success", True):
            raise HTTPException(status_code=400, detail=res.get("message"))
        return res
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error updating paper order history: {str(e)}")

@router.delete("/order-history/{log_id}")
def api_delete_paper_order_history(log_id: int):
    """Deletes an audit paper trade order history record from PostgreSQL."""
    try:
        res = delete_paper_order_history(log_id)
        if not res.get("success", True):
            raise HTTPException(status_code=400, detail=res.get("message"))
        return res
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error deleting paper order history: {str(e)}")

@router.post("/reset")
def api_reset_paper_account():
    """Resets paper trading account: closes open MT5 positions and truncates local logs."""
    try:
        return reset_paper_account()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Reset paper account error: {str(e)}")

@router.get("/export")
def api_export_paper_trades(
    format: str = Query("csv", description="Export format: 'csv' or 'json'")
):
    """Exports MT5 paper trade deal history as a downloadable CSV or JSON file."""
    format_clean = format.lower().strip()
    if format_clean not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="Invalid format. Supported formats are 'csv' and 'json'.")

    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        schema = settings.mimir_schema
        cur.execute(f"""
            SELECT id, mt5_ticket, ticker, action, quantity, entry_price, exit_price,
                   entry_time, exit_time, exit_reason, realized_pnl, realized_pnl_pct, notes
            FROM {schema}.mimir_paper_trade_log
            ORDER BY id ASC
        """)
        rows = cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database query error: {str(e)}")
    finally:
        cur.close()
        conn.close()

    gmt_plus_7 = timezone(timedelta(hours=7))
    records = []
    for r in rows:
        def fmt_dt(dt):
            if not dt:
                return ""
            if dt.tzinfo is None:
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            return dt.astimezone(gmt_plus_7).strftime("%Y-%m-%d %H:%M:%S")

        entry_time_str = fmt_dt(r.get("entry_time"))
        exit_time_str = fmt_dt(r.get("exit_time"))

        records.append({
            "id": r["id"],
            "ticket": r.get("mt5_ticket"),
            "ticker": (r.get("ticker") or "").upper(),
            "action": (r.get("action") or "").upper(),
            "quantity": float(r["quantity"]) if r.get("quantity") is not None else 0.0,
            "entry_price": float(r["entry_price"]) if r.get("entry_price") is not None else 0.0,
            "exit_price": float(r["exit_price"]) if r.get("exit_price") is not None else 0.0,
            "entry_time": entry_time_str,
            "exit_time": exit_time_str,
            "exit_reason": r.get("exit_reason") or "",
            "realized_pnl": float(r["realized_pnl"]) if r.get("realized_pnl") is not None else 0.0,
            "realized_pnl_pct": float(r["realized_pnl_pct"]) if r.get("realized_pnl_pct") is not None else 0.0,
            "notes": r.get("notes") or ""
        })

    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    if format_clean == "json":
        json_content = json.dumps(records, indent=2)
        filename = f"mimir_paper_trade_history_{now_str}.json"
        return Response(
            content=json_content,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow([
        "ID",
        "Ticket",
        "Ticker",
        "Action",
        "Quantity",
        "Entry Price ($)",
        "Exit Price ($)",
        "Entry Time (GMT+7)",
        "Exit Time (GMT+7)",
        "Exit Reason",
        "Realized P&L ($)",
        "Realized P&L (%)",
        "Notes"
    ])
    for rec in records:
        writer.writerow([
            rec["id"],
            rec["ticket"],
            rec["ticker"],
            rec["action"],
            rec["quantity"],
            rec["entry_price"],
            rec["exit_price"],
            rec["entry_time"],
            rec["exit_time"],
            rec["exit_reason"],
            rec["realized_pnl"],
            rec["realized_pnl_pct"],
            rec["notes"]
        ])

    csv_content = output.getvalue()
    filename = f"mimir_paper_trade_history_{now_str}.csv"
    return Response(
        content=csv_content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

