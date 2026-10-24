# backend/app/routers/trade_alerts.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timezone, timedelta

from ..database import get_db_connection_dict, get_db_connection
from ..config import get_settings
from ..analytics.mt5_bridge import send_market_order, MIMIR_MAGIC
from ..analytics.paper_trader import get_paper_config

router = APIRouter()
settings = get_settings()

class TradeSignalResponse(BaseModel):
    id: int
    ticker: str
    signal_type: str
    trigger_price: float
    rsi_value: Optional[float]
    sentiment_score: Optional[float]
    support_level: Optional[float]
    resistance_level: Optional[float]
    reason: Optional[str]
    catalyst_type: Optional[str] = "SENTIMENT_CATALYST"
    holding_period: Optional[str] = "Swing Horizon (3-7 days)"
    investment_thesis: Optional[str] = None
    headline: Optional[str] = None
    target_price: Optional[float] = None
    stop_loss: Optional[float] = None
    conviction_score: Optional[float] = None
    status: str
    created_at: datetime
    acted_at: Optional[datetime]
    win_rate: Optional[float] = None
    avg_pnl: Optional[float] = None

class ActionPayload(BaseModel):
    quantity: float = 10.0  # Default to 10 shares

@router.get("/alerts/pending", response_model=List[TradeSignalResponse])
def get_pending_alerts():
    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        # Auto-expire signals older than 24 hours to keep the queue clean
        cur.execute(f"""
            UPDATE {settings.mimir_schema}.mimir_trade_signals
            SET status = 'EXPIRED'
            WHERE status = 'PENDING' AND created_at < NOW() - INTERVAL '24 hours'
        """)
        conn.commit()

        cur.execute(f"""
            SELECT s.id, s.ticker, s.signal_type, s.trigger_price, s.rsi_value, s.sentiment_score, 
                   s.support_level, s.resistance_level, s.reason,
                   s.catalyst_type, s.holding_period, s.investment_thesis, s.headline,
                   s.target_price, s.stop_loss, s.conviction_score,
                   s.status, s.created_at, s.acted_at,
                   p.win_rate, p.avg_pnl
            FROM {settings.mimir_schema}.mimir_trade_signals s
            LEFT JOIN {settings.mimir_schema}.mimir_ticker_parameters p ON s.ticker = p.ticker
            WHERE s.status = 'PENDING'
            ORDER BY s.created_at DESC
        """)
        rows = cur.fetchall()
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cur.close()
        conn.close()

@router.get("/alerts/sector-rotation")
@router.get("/sector-rotation")
def get_sector_rotation():
    """
    Returns the live Sector Rotation Matrix across all 11 SPDR sectors,
    quantifying 5-day and 20-day Relative Strength vs SPY, Chaikin Money Flow (CMF),
    and DeepSeek news narrative velocity.
    """
    try:
        from ..services.sector_rotation_service import get_sector_rotation_matrix
        matrix = get_sector_rotation_matrix()
    except Exception as e:
        return {"status": "error", "message": str(e), "data": {"sectors": [], "spy": {}}}

@router.get("/alerts/war-rig/evaluate/{ticker}")
@router.get("/war-rig/evaluate/{ticker}")
def evaluate_war_rig_ticker(ticker: str):
    """
    Evaluates a specific equity ticker through the unified War Rig transmission:
    - Cylinder 1: Macro & Sector Inflows (Max 25 pts)
    - Cylinder 2: Catalyst V8 Twin-Turbo (Pre-Earnings + Supply Chain Spillovers) (Max 50 pts)
    - Cylinder 3: Microstructure & Asymmetry Gate (Max 25 pts + R/R >= 2.5:1 Gate)
    """
    try:
        from ..analytics.war_rig_engine import WarRigEngine
        war_rig = WarRigEngine()
        sig = war_rig.evaluate_war_rig_candidate(ticker.strip().upper())
        if sig:
            return {"status": "success", "converged": True, "signal": sig}
        else:
            # Provide diagnostic cylinder breakdown even if it didn't pass 75% gate
            c1_score, c1_details = war_rig.evaluate_cylinder_1_macro_sector(ticker.strip().upper())
            c2_score, c2_details = war_rig.evaluate_cylinder_2_catalysts(ticker.strip().upper())
            return {
                "status": "success",
                "converged": False,
                "reason": "Did not meet >=75% conviction threshold or failed asymmetry gate.",
                "cylinder_1_macro_sector": {"score": c1_score, "details": c1_details},
                "cylinder_2_catalysts": {"score": c2_score, "details": c2_details}
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"War Rig evaluation error: {str(e)}")

@router.post("/alerts/war-rig/scan")
@router.post("/war-rig/scan")
def trigger_war_rig_scan(top_n: int = 10):
    """
    Triggers the single-shaft War Rig Alpha Scan across the MIMIR universe.
    Only signals that pass all 3 cylinders with >= 75% conviction and >= 2.5:1 R/R
    are inserted into mimir_trade_signals.
    """
    try:
        from ..analytics.war_rig_engine import run_war_rig_scan
        signals = run_war_rig_scan(top_n=top_n)
        return {"status": "success", "count": len(signals), "signals": signals}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"War Rig scan error: {str(e)}")

@router.post("/alerts/{alert_id}/approve", response_model=TradeSignalResponse)
def approve_alert(alert_id: int, payload: ActionPayload):
    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        # 1. Fetch the alert
        cur.execute(f"""
            SELECT * FROM {settings.mimir_schema}.mimir_trade_signals
            WHERE id = %s AND status = 'PENDING'
        """, (alert_id,))
        alert = cur.fetchone()
        if not alert:
            raise HTTPException(status_code=404, detail="Pending trade signal not found.")
            
        ticker = alert["ticker"]
        signal_type = alert["signal_type"].upper()
        price = float(alert["trigger_price"])
        
        # 2. Execute order directly in MT5 using centralized paper trader
        from ..analytics.paper_trader import execute_single_paper_trade
        order_res = execute_single_paper_trade(
            signal_id=alert_id,
            ticker=ticker,
            signal_type=signal_type,
            trigger_price=price,
            target_price=float(alert["target_price"]) if alert.get("target_price") else None,
            stop_loss=float(alert["stop_loss"]) if alert.get("stop_loss") else None,
            conviction_score=float(alert.get("conviction_score") or 0.5),
            catalyst_type=alert.get("catalyst_type"),
            reason=alert.get("reason"),
            fixed_quantity=payload.quantity,
            force=True,
            conn=conn
        )

        if not order_res.get("success"):
            err_msg = order_res.get("message", "MT5 order placement failed.")
            raise HTTPException(status_code=400, detail=f"MT5 Order Execution Failed: {err_msg}")

        cur.execute(f"""
            SELECT id, ticker, signal_type, trigger_price, rsi_value, sentiment_score, 
                   support_level, resistance_level, reason, status, created_at, acted_at
            FROM {settings.mimir_schema}.mimir_trade_signals
            WHERE id = %s
        """, (alert_id,))
        
        updated_alert = cur.fetchone()
        conn.commit()
        return updated_alert
    except HTTPException as he:
        conn.rollback()
        raise he
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cur.close()
        conn.close()

@router.post("/alerts/{alert_id}/reject", response_model=TradeSignalResponse)
def reject_alert(alert_id: int):
    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        # Fetch alert to check if exists
        cur.execute(f"""
            SELECT id FROM {settings.mimir_schema}.mimir_trade_signals
            WHERE id = %s AND status = 'PENDING'
        """, (alert_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Pending trade signal not found.")
            
        gmt_plus_7 = timezone(timedelta(hours=7))
        now_local = datetime.now(gmt_plus_7)
        
        cur.execute(f"""
            UPDATE {settings.mimir_schema}.mimir_trade_signals
            SET status = 'REJECTED', acted_at = %s
            WHERE id = %s
            RETURNING id, ticker, signal_type, trigger_price, rsi_value, sentiment_score, 
                      support_level, resistance_level, reason, status, created_at, acted_at
        """, (now_local, alert_id))
        
        updated_alert = cur.fetchone()
        conn.commit()
        return updated_alert
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cur.close()
        conn.close()

@router.post("/alerts/{alert_id}/read", response_model=TradeSignalResponse)
def mark_alert_read(alert_id: int):
    """Marks a trade signal notification as read so it is dismissed and will not pop up again."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT id FROM {settings.mimir_schema}.mimir_trade_signals
            WHERE id = %s
        """, (alert_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Trade signal not found.")
            
        gmt_plus_7 = timezone(timedelta(hours=7))
        now_local = datetime.now(gmt_plus_7)
        
        cur.execute(f"""
            UPDATE {settings.mimir_schema}.mimir_trade_signals
            SET status = 'READ', acted_at = %s
            WHERE id = %s
            RETURNING id, ticker, signal_type, trigger_price, rsi_value, sentiment_score, 
                      support_level, resistance_level, reason, status, created_at, acted_at
        """, (now_local, alert_id))
        
        updated_alert = cur.fetchone()
        conn.commit()
        return updated_alert
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cur.close()
        conn.close()

@router.post("/alerts/mark-all-read")
def mark_all_alerts_read():
    """Marks all pending trade signal notifications as read."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        gmt_plus_7 = timezone(timedelta(hours=7))
        now_local = datetime.now(gmt_plus_7)
        cur.execute(f"""
            UPDATE {settings.mimir_schema}.mimir_trade_signals
            SET status = 'READ', acted_at = %s
            WHERE status = 'PENDING'
        """, (now_local,))
        read_count = cur.rowcount
        conn.commit()
        return {"message": f"Marked {read_count} signals as read.", "read_count": read_count}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cur.close()
        conn.close()

class BulkDismissPayload(BaseModel):
    min_win_rate: float = 55.0

@router.post("/alerts/bulk-dismiss")
def bulk_dismiss_low_conviction_alerts(payload: BulkDismissPayload):
    """Dismisses all pending signals that have estimated win rates below min_win_rate or no profile."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        gmt_plus_7 = timezone(timedelta(hours=7))
        now_local = datetime.now(gmt_plus_7)
        
        cur.execute(f"""
            UPDATE {settings.mimir_schema}.mimir_trade_signals s
            SET status = 'REJECTED', acted_at = %s
            FROM {settings.mimir_schema}.mimir_ticker_parameters p
            WHERE s.ticker = p.ticker 
              AND s.status = 'PENDING'
              AND (p.win_rate IS NULL OR p.win_rate < %s)
        """, (now_local, payload.min_win_rate))
        dismissed_count = cur.rowcount
        conn.commit()
        return {"message": f"Successfully dismissed {dismissed_count} low-conviction signals.", "dismissed": dismissed_count}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cur.close()
        conn.close()

def evaluate_tick_technicals(price_cache):
    """
    [PERMANENTLY DEPRECATED]
    Standalone 1-minute RSI and Breakout signals have been deactivated per institutional mandate.
    Empirical audit proved standalone 1-min technical breakouts/reversions produced knife-catching 
    losses and low win rates. Microstructure is now strictly utilized as an invalidation gate 
    inside the unified War Rig Engine (war_rig_engine.py), never as an unvetted standalone emitter.
    """
    pass
