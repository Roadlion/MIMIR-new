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
                   s.support_level, s.resistance_level, s.reason, s.status, s.created_at, s.acted_at,
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
    """Event-driven technical analysis evaluation over the live 1-min in-memory cache."""
    import pandas as pd
    from ..analytics.technical_analysis import analyze_technical_indicators
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        signals_generated = 0
        for ticker, ticks in price_cache.items():
            if len(ticks) < 50:
                continue
                
            df = pd.DataFrame(list(ticks))
            current_price = df.iloc[-1]['close']
            
            techs = analyze_technical_indicators(df)
            
            resistance = techs["resistance"]
            support = techs["support"]
            rsi = round(techs["rsi"], 2)
            
            signal_type = None
            reason = ""
            
            # Simple 1-min breakout/reversion logic (higher conviction bounds)
            if current_price >= resistance and rsi > 65:
                signal_type = "BUY"
                reason = f"Strong Resistance breakout ({resistance}) with bullish RSI ({rsi})"
            elif rsi <= 20:
                signal_type = "BUY"
                reason = f"Extreme oversold reversion (RSI {rsi})"
            elif current_price <= support and rsi < 35:
                signal_type = "SELL"
                reason = f"Support breakdown ({support}) with bearish RSI ({rsi})"
            elif rsi >= 80:
                signal_type = "SELL"
                reason = f"Extreme overbought reversion (RSI {rsi})"
                
            if signal_type:
                # Prevent spam: limit 1 alert per ticker every 60 minutes
                cur.execute(f"""
                    SELECT id FROM {settings.mimir_schema}.mimir_trade_signals 
                    WHERE ticker = %s AND status = 'PENDING' 
                    AND created_at >= NOW() - INTERVAL '60 minutes'
                """, (ticker,))
                
                if not cur.fetchone():
                    cur.execute(f"""
                        INSERT INTO {settings.mimir_schema}.mimir_trade_signals
                        (ticker, signal_type, trigger_price, rsi_value, support_level, resistance_level, reason, status, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING', NOW())
                    """, (ticker, signal_type, float(current_price), float(rsi), float(support), float(resistance), reason))
                    signals_generated += 1
                    
        conn.commit()
        if signals_generated > 0:
            print(f"[TECHNICAL ALERTS] Generated {signals_generated} real-time technical alerts.")
    except Exception as e:
        conn.rollback()
        print(f"[TECHNICAL ALERTS ERROR] {e}")
    finally:
        cur.close()
        conn.close()
