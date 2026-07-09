# backend/app/routers/trade_alerts.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timezone, timedelta

from ..database import get_db_connection_dict, get_db_connection
from ..config import get_settings

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
    status: str
    created_at: datetime
    acted_at: Optional[datetime]

class ActionPayload(BaseModel):
    quantity: float = 10.0  # Default to 10 shares

@router.get("/alerts/pending", response_model=List[TradeSignalResponse])
def get_pending_alerts():
    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT id, ticker, signal_type, trigger_price, rsi_value, sentiment_score, 
                   support_level, resistance_level, reason, status, created_at, acted_at
            FROM {settings.mimir_schema}.mimir_trade_signals
            WHERE status = 'PENDING'
            ORDER BY created_at DESC
        """)
        rows = cur.fetchall()
        return rows
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cur.close()
        conn.close()

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
        signal_type = alert["signal_type"]
        price = float(alert["trigger_price"])
        
        # 2. If it's a SELL, check quantity in portfolio
        if signal_type == "SELL":
            # Check current position size
            cur.execute(f"""
                SELECT transaction_type, quantity
                FROM {settings.mimir_schema}.mimir_portfolio
                WHERE ticker = %s
            """, (ticker,))
            existing_txs = cur.fetchall()
            current_qty = 0.0
            for etx in existing_txs:
                etype = etx["transaction_type"].upper()
                eqty = float(etx["quantity"])
                if etype == "BUY":
                    current_qty += eqty
                elif etype == "SELL":
                    current_qty -= eqty
            
            if payload.quantity > current_qty:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Cannot execute SELL for {payload.quantity} shares of {ticker}. You only own {current_qty} shares."
                )
                
        # 3. Create the Shadow Portfolio transaction
        gmt_plus_7 = timezone(timedelta(hours=7))
        now_local = datetime.now(gmt_plus_7)
        
        cur.execute(f"""
            INSERT INTO {settings.mimir_schema}.mimir_portfolio (ticker, order_date, buy_price, quantity, transaction_type)
            VALUES (%s, %s, %s, %s, %s)
        """, (ticker, now_local, price, payload.quantity, signal_type))
        
        # 4. Update the signal status
        cur.execute(f"""
            UPDATE {settings.mimir_schema}.mimir_trade_signals
            SET status = 'APPROVED', acted_at = %s
            WHERE id = %s
            RETURNING id, ticker, signal_type, trigger_price, rsi_value, sentiment_score, 
                      support_level, resistance_level, reason, status, created_at, acted_at
        """, (now_local, alert_id))
        
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
