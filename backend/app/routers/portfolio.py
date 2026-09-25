# backend/app/routers/portfolio.py
from fastapi import APIRouter, HTTPException, Query, Depends, Response
from pydantic import BaseModel
from typing import List, Optional, Dict
from datetime import datetime, timezone
from decimal import Decimal
import requests
import json
import csv
import io
import re
import math
import time
import pandas as pd
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor

from ..database import get_db_connection_dict, get_db_connection
from ..config import get_settings
from ..auth import get_optional_current_user, get_current_user
from ..sentiment.llm_client import send_chat_completion
from ..analytics.technical_analysis import find_support_resistance
from ..services.sector_rotation_service import get_ticker_sector_tailwinds

router = APIRouter()
settings = get_settings()

class TransactionCreate(BaseModel):
    ticker: str
    order_date: datetime
    buy_price: float
    quantity: float
    transaction_type: str = "BUY"
    brokerage_fee: Optional[float] = 0.0
    regulatory_fee: Optional[float] = 0.0
    other_fee: Optional[float] = 0.0

class TransactionResponse(BaseModel):
    id: int
    ticker: str
    order_date: datetime
    buy_price: float
    quantity: float
    transaction_type: str
    created_at: datetime
    brokerage_fee: float = 0.0
    regulatory_fee: float = 0.0
    other_fee: float = 0.0

class TransactionUpdate(BaseModel):
    ticker: str
    order_date: datetime
    buy_price: float
    quantity: float
    transaction_type: str
    brokerage_fee: Optional[float] = 0.0
    regulatory_fee: Optional[float] = 0.0
    other_fee: Optional[float] = 0.0

class HoldingDetail(BaseModel):
    ticker: str
    quantity: float
    avg_buy_price: float
    total_cost: float
    current_price: float
    current_value: float
    profit_loss: float
    profit_loss_pct: float
    realized_pl: float
    dividends_received: float = 0.0
    transactions: List[Dict]

class PortfolioSummary(BaseModel):
    holdings: Dict[str, HoldingDetail]
    total_cost: float
    total_value: float
    total_profit_loss: float
    total_profit_loss_pct: float
    total_realized_pl: float
    total_dividends: float = 0.0
    grand_total_pl: float
    total_api_costs: float = 0.0

# Helper to fetch current prices quickly from database with fallback
def fetch_current_prices(tickers: List[str]) -> Dict[str, float]:
    if not tickers:
        return {}
    prices = {}
    clean_map = {t: t.strip().lstrip('$').upper() for t in tickers}
    unique_symbols = list(set(clean_map.values()))

    # 1. Fast batch lookup in mimir_latest_prices (<2ms)
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"""
            SELECT ticker, latest_price
            FROM {settings.mimir_schema}.mimir_latest_prices
            WHERE ticker = ANY(%s)
        """, (unique_symbols,))
        for row in cur.fetchall():
            sym = row[0].upper()
            prices[sym] = float(row[1]) if row[1] is not None else 0.0
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[PORTFOLIO] mimir_latest_prices lookup error: {e}")

    # 2. Check MT5 for any missing symbols
    missing = [sym for sym in unique_symbols if sym not in prices or prices[sym] <= 0]
    if missing:
        try:
            import MetaTrader5 as mt5
            from ..analytics.mt5_bridge import resolve_mt5_symbol
            for sym in missing:
                mt5_sym = resolve_mt5_symbol(sym)
                if mt5_sym:
                    tick = mt5.symbol_info_tick(mt5_sym)
                    if tick:
                        price = float(tick.last if tick.last > 0 else (tick.bid if tick.bid > 0 else tick.ask))
                        if price > 0:
                            prices[sym] = price
        except Exception:
            pass

    # 3. For any remaining missing, fallback to fast yfinance history (1 thread per call, safe)
    still_missing = [sym for sym in unique_symbols if sym not in prices or prices[sym] <= 0]
    for sym in still_missing:
        try:
            t = yf.Ticker(sym)
            hist = t.history(period="1d")
            if not hist.empty:
                prices[sym] = float(hist["Close"].iloc[-1])
            else:
                prices[sym] = 0.0
        except Exception:
            prices[sym] = 0.0

    # Map back to original tickers requested
    return {orig: prices.get(clean, 0.0) for orig, clean in clean_map.items()}

@router.get("/portfolio/tickers")
def get_portfolio_tickers(current_user: Optional[dict] = Depends(get_optional_current_user)):
    user_id = current_user["id"] if current_user else 1
    """Returns a list of distinct active tickers currently held in real and paper portfolios."""
    tickers = set()
    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        schema = settings.mimir_schema
        # 1. Real portfolio active tickers
        cur.execute(f"""
            SELECT ticker, transaction_type, quantity
            FROM {schema}.mimir_portfolio
            WHERE user_id = %s AND (source IS NULL OR source = 'MANUAL' OR source = '')
        """, (user_id,))
        rows = cur.fetchall()
        holdings = {}
        for r in rows:
            t = r["ticker"].upper()
            q = float(r["quantity"])
            ttype = (r["transaction_type"] or "BUY").upper()
            if t not in holdings:
                holdings[t] = 0.0
            if ttype == "BUY":
                holdings[t] += q
            elif ttype == "SELL":
                holdings[t] -= q
            
            holdings[t] = round(holdings[t], 8)
            if holdings[t] <= 0:
                holdings[t] = 0.0
        for t, qty in holdings.items():
            if qty > 0.0001:
                tickers.add(t)

        # 2. Paper portfolio active tickers
        cur.execute(f"""
            SELECT ticker, transaction_type, quantity
            FROM {schema}.mimir_paper_portfolio
        """)
        rows_p = cur.fetchall()
        holdings_p = {}
        for r in rows_p:
            t = r["ticker"].upper()
            q = float(r["quantity"])
            ttype = (r["transaction_type"] or "BUY").upper()
            if t not in holdings_p:
                holdings_p[t] = 0.0
            if ttype == "BUY":
                holdings_p[t] += q
            elif ttype == "SELL":
                holdings_p[t] -= q
            
            holdings_p[t] = round(holdings_p[t], 8)
            if holdings_p[t] <= 0:
                holdings_p[t] = 0.0
        for t, qty in holdings_p.items():
            if qty > 0.0001:
                tickers.add(t)

    except Exception as e:
        print(f"[PORTFOLIO TICKERS ERROR] {e}")
    finally:
        cur.close()
        conn.close()

    return {"tickers": sorted(list(tickers))}

@router.get("/portfolio", response_model=PortfolioSummary)
def get_portfolio(current_user: Optional[dict] = Depends(get_optional_current_user)):
    user_id = current_user["id"] if current_user else 1

    # Fetch total API costs
    total_api_costs = 0.0
    conn_cost = get_db_connection_dict()
    cur_cost = conn_cost.cursor()
    try:
        cur_cost.execute(f"SELECT SUM(cost_usd) as sum_cost FROM {settings.mimir_schema}.mimir_api_cost_ledger WHERE user_id = %s", (user_id,))
        row = cur_cost.fetchone()
        if row and row["sum_cost"] is not None:
            total_api_costs = float(row["sum_cost"])
    except Exception:
        pass
    finally:
        cur_cost.close()
        conn_cost.close()

    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    # Fetch all real/manual transactions for this user
    cur.execute(f"""
        SELECT id, ticker, order_date, buy_price, quantity, created_at, transaction_type, brokerage_fee, regulatory_fee, other_fee
        FROM {settings.mimir_schema}.mimir_portfolio
        WHERE user_id = %s AND (source IS NULL OR source = 'MANUAL' OR source = '')
        ORDER BY order_date DESC
    """, (user_id,))
    transactions = cur.fetchall()
    cur.close()
    conn.close()
    
    if not transactions:
        return {
            "holdings": {},
            "total_cost": 0.0,
            "total_value": 0.0,
            "total_profit_loss": 0.0,
            "total_profit_loss_pct": 0.0,
            "total_realized_pl": 0.0,
            "total_dividends": 0.0,
            "grand_total_pl": 0.0,
            "total_api_costs": total_api_costs
        }
        
    # Group transactions by ticker
    raw_holdings = {}
    for tx in transactions:
        ticker = tx["ticker"].upper()
        if ticker not in raw_holdings:
            raw_holdings[ticker] = []
        raw_holdings[ticker].append(tx)
        
    # Get current prices from yfinance
    tickers = list(raw_holdings.keys())
    current_prices = fetch_current_prices(tickers) or {}
    
    holdings = {}
    total_cost = 0.0
    total_value = 0.0
    total_realized_pl = 0.0
    total_dividends = 0.0
    
    for ticker, txs in raw_holdings.items():
        # Sort chronologically to compute weighted average cost basis and realized P&L
        txs_sorted = sorted(txs, key=lambda x: x["order_date"])
        
        qty_sum = 0.0
        avg_buy = 0.0
        realized_pl = 0.0
        dividends_received = 0.0
        
        for tx in txs_sorted:
            tx_qty = float(tx["quantity"])
            tx_price = float(tx["buy_price"])
            tx_type = tx.get("transaction_type", "BUY").upper()
            
            tx_brokerage = float(tx.get("brokerage_fee") or 0.0)
            tx_regulatory = float(tx.get("regulatory_fee") or 0.0)
            tx_other = float(tx.get("other_fee") or 0.0)
            total_tx_fees = tx_brokerage + tx_regulatory + tx_other
            
            if tx_type == "BUY":
                # For BUY, fees increase the cost basis
                tx_cost = tx_qty * tx_price + total_tx_fees
                if qty_sum + tx_qty > 0:
                    avg_buy = (qty_sum * avg_buy + tx_cost) / (qty_sum + tx_qty)
                else:
                    avg_buy = 0.0
                qty_sum += tx_qty
            elif tx_type == "SELL":
                # For SELL, fees decrease the realized P&L / proceeds
                realized_pl += tx_qty * (tx_price - avg_buy) - total_tx_fees
                qty_sum -= tx_qty
            elif tx_type == "DIVIDEND":
                # For DIVIDEND, fees reduce the payout received
                div_net = (tx_qty * tx_price) - total_tx_fees
                dividends_received += div_net

            qty_sum = round(qty_sum, 8)
            if qty_sum <= 0.00001:
                qty_sum = 0.0
                avg_buy = 0.0

        if qty_sum <= 0.00001:
            qty_sum = 0.0
            avg_buy = 0.0

        curr_price = current_prices.get(ticker, 0.0)
        
        # If current price is 0.0, fallback to last buy price to prevent weird profit/loss
        if curr_price == 0.0:
            buys = [float(tx["buy_price"]) for tx in txs_sorted if tx.get("transaction_type", "BUY").upper() == "BUY"]
            if buys:
                curr_price = buys[-1]
            elif txs_sorted:
                curr_price = float(txs_sorted[-1]["buy_price"])
            
        cost_sum = qty_sum * avg_buy if qty_sum > 0 else 0.0
        curr_val = qty_sum * curr_price if qty_sum > 0 else 0.0
        pl = curr_val - cost_sum
        pl_pct = (pl / cost_sum * 100) if cost_sum > 0 else 0.0
        
        total_cost += cost_sum
        total_value += curr_val
        total_realized_pl += realized_pl
        total_dividends += dividends_received
        
        holdings[ticker] = {
            "ticker": ticker,
            "quantity": qty_sum,
            "avg_buy_price": avg_buy,
            "total_cost": cost_sum,
            "current_price": curr_price,
            "current_value": curr_val,
            "profit_loss": pl,
            "profit_loss_pct": pl_pct,
            "realized_pl": realized_pl,
            "dividends_received": dividends_received,
            "transactions": [
                {
                    "id": tx["id"],
                    "order_date": tx["order_date"],
                    "buy_price": float(tx["buy_price"]),
                    "quantity": float(tx["quantity"]),
                    "transaction_type": tx.get("transaction_type", "BUY"),
                    "created_at": tx["created_at"],
                    "brokerage_fee": float(tx.get("brokerage_fee") or 0.0),
                    "regulatory_fee": float(tx.get("regulatory_fee") or 0.0),
                    "other_fee": float(tx.get("other_fee") or 0.0)
                }
                for tx in txs
            ]
        }
        
    total_pl = total_value - total_cost
    total_pl_pct = (total_pl / total_cost * 100) if total_cost > 0 else 0.0
    grand_total = total_pl + total_realized_pl + total_dividends
    
    return {
        "holdings": holdings,
        "total_cost": total_cost,
        "total_value": total_value,
        "total_profit_loss": total_pl,
        "total_profit_loss_pct": total_pl_pct,
        "total_realized_pl": total_realized_pl,
        "total_dividends": total_dividends,
        "grand_total_pl": grand_total,
        "total_api_costs": total_api_costs
    }

@router.post("/portfolio", response_model=TransactionResponse)
def add_transaction(tx: TransactionCreate, current_user: Optional[dict] = Depends(get_optional_current_user)):
    user_id = current_user["id"] if current_user else 1

    # Enforce GMT+7 (Asia/Bangkok) timezone for order date
    from datetime import timezone, timedelta
    gmt_plus_7 = timezone(timedelta(hours=7))
    if tx.order_date.tzinfo is None:
        localized_date = tx.order_date.replace(tzinfo=gmt_plus_7)
    else:
        localized_date = tx.order_date.astimezone(gmt_plus_7)
        
    # Check current quantity for SELL validation
    if tx.transaction_type.upper() == "SELL":
        conn = get_db_connection_dict()
        cur = conn.cursor()
        try:
            cur.execute(f"""
                SELECT transaction_type, quantity
                FROM {settings.mimir_schema}.mimir_portfolio
                WHERE user_id = %s AND ticker = %s
            """, (user_id, tx.ticker.upper().strip()))
            existing_txs = cur.fetchall()
            current_qty = Decimal("0")
            for etx in existing_txs:
                etype = (etx["transaction_type"] or "BUY").upper()
                eqty = Decimal(str(etx["quantity"])) if etx.get("quantity") is not None else Decimal("0")
                if etype == "BUY":
                    current_qty += eqty
                elif etype == "SELL":
                    current_qty -= eqty
            
            sell_qty = Decimal(str(tx.quantity))
            # If sell_qty is within 0.00001 of current_qty (e.g. user selling full position with float rounding),
            # snap sell_qty exactly to current_qty to eliminate floating-point dust leftovers
            if abs(sell_qty - current_qty) < Decimal("0.00001"):
                tx.quantity = float(current_qty)
            elif sell_qty > current_qty + Decimal("0.00001"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot sell {tx.quantity} shares of {tx.ticker}. You only own {float(current_qty)} shares."
                )
        finally:
            cur.close()
            conn.close()

    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        cur.execute(f"""
            INSERT INTO {settings.mimir_schema}.mimir_portfolio (user_id, ticker, order_date, buy_price, quantity, transaction_type, brokerage_fee, regulatory_fee, other_fee)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, ticker, order_date, buy_price, quantity, transaction_type, created_at, brokerage_fee, regulatory_fee, other_fee
        """, (user_id, tx.ticker.upper().strip(), localized_date, tx.buy_price, round(tx.quantity, 8), tx.transaction_type.upper(), tx.brokerage_fee or 0.0, tx.regulatory_fee or 0.0, tx.other_fee or 0.0))
        new_tx = cur.fetchone()
        conn.commit()
        return new_tx
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cur.close()
        conn.close()

@router.put("/portfolio/{tx_id}", response_model=TransactionResponse)
def edit_transaction(tx_id: int, tx: TransactionUpdate, current_user: Optional[dict] = Depends(get_optional_current_user)):
    user_id = current_user["id"] if current_user else 1

    from datetime import timezone, timedelta
    gmt_plus_7 = timezone(timedelta(hours=7))
    if tx.order_date.tzinfo is None:
        localized_date = tx.order_date.replace(tzinfo=gmt_plus_7)
    else:
        localized_date = tx.order_date.astimezone(gmt_plus_7)

    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        # 1. Fetch existing transaction to get old ticker
        cur.execute(f"SELECT ticker FROM {settings.mimir_schema}.mimir_portfolio WHERE id = %s AND user_id = %s", (tx_id, user_id))
        old_tx = cur.fetchone()
        if not old_tx:
            raise HTTPException(status_code=404, detail="Transaction not found.")
        old_ticker = old_tx["ticker"].upper().strip()
        new_ticker = tx.ticker.upper().strip()

        # 2. Check running inventory for old_ticker (excluding the edited transaction)
        cur.execute(f"""
            SELECT id, transaction_type, quantity, order_date
            FROM {settings.mimir_schema}.mimir_portfolio
            WHERE user_id = %s AND ticker = %s AND id != %s
        """, (user_id, old_ticker, tx_id))
        old_ticker_txs = cur.fetchall()

        if old_ticker == new_ticker:
            # Add the proposed edited transaction to validate the new state of this ticker
            proposed_tx = {
                "id": tx_id,
                "transaction_type": tx.transaction_type.upper(),
                "quantity": tx.quantity,
                "order_date": localized_date
            }
            all_proposed = old_ticker_txs + [proposed_tx]
            all_proposed_sorted = sorted(all_proposed, key=lambda x: x["order_date"])
            
            qty_running = Decimal("0")
            for item in all_proposed_sorted:
                itype = (item.get("transaction_type") or "BUY").upper()
                iqty = Decimal(str(item["quantity"]))
                if itype == "BUY":
                    qty_running += iqty
                elif itype == "SELL":
                    qty_running -= iqty
                if qty_running < -Decimal("0.00001"):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Proposed changes would result in a negative holding quantity ({float(qty_running)}) for {old_ticker} at {item['order_date']}."
                    )
        else:
            # Validate old ticker's inventory (excluding edited transaction)
            old_sorted = sorted(old_ticker_txs, key=lambda x: x["order_date"])
            qty_running_old = Decimal("0")
            for item in old_sorted:
                itype = (item.get("transaction_type") or "BUY").upper()
                iqty = Decimal(str(item["quantity"]))
                if itype == "BUY":
                    qty_running_old += iqty
                elif itype == "SELL":
                    qty_running_old -= iqty
                if qty_running_old < -Decimal("0.00001"):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Removing this transaction would result in negative holding quantity ({float(qty_running_old)}) for {old_ticker} at {item['order_date']}."
                    )

            # Validate new ticker's inventory (including proposed transaction)
            cur.execute(f"""
                SELECT id, transaction_type, quantity, order_date
                FROM {settings.mimir_schema}.mimir_portfolio
                WHERE user_id = %s AND ticker = %s
            """, (user_id, new_ticker))
            new_ticker_txs = cur.fetchall()
            proposed_tx = {
                "id": tx_id,
                "transaction_type": tx.transaction_type.upper(),
                "quantity": tx.quantity,
                "order_date": localized_date
            }
            all_proposed_new = new_ticker_txs + [proposed_tx]
            new_sorted = sorted(all_proposed_new, key=lambda x: x["order_date"])
            qty_running_new = Decimal("0")
            for item in new_sorted:
                itype = (item.get("transaction_type") or "BUY").upper()
                iqty = Decimal(str(item["quantity"]))
                if itype == "BUY":
                    qty_running_new += iqty
                elif itype == "SELL":
                    qty_running_new -= iqty
                if qty_running_new < -Decimal("0.00001"):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Proposed changes would result in a negative holding quantity ({float(qty_running_new)}) for {new_ticker} at {item['order_date']}."
                    )

        # 3. Perform the update
        cur.execute(f"""
            UPDATE {settings.mimir_schema}.mimir_portfolio
            SET ticker = %s, order_date = %s, buy_price = %s, quantity = %s, transaction_type = %s,
                brokerage_fee = %s, regulatory_fee = %s, other_fee = %s
            WHERE id = %s AND user_id = %s
            RETURNING id, ticker, order_date, buy_price, quantity, transaction_type, created_at, brokerage_fee, regulatory_fee, other_fee
        """, (new_ticker, localized_date, tx.buy_price, round(tx.quantity, 8), tx.transaction_type.upper(),
              tx.brokerage_fee or 0.0, tx.regulatory_fee or 0.0, tx.other_fee or 0.0, tx_id, user_id))
        updated_tx = cur.fetchone()
        conn.commit()
        return updated_tx
    except HTTPException:
        conn.rollback()
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cur.close()
        conn.close()

@router.delete("/portfolio/{tx_id}")
def delete_transaction(tx_id: int, current_user: Optional[dict] = Depends(get_optional_current_user)):
    user_id = current_user["id"] if current_user else 1

    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        cur.execute(f"""
            DELETE FROM {settings.mimir_schema}.mimir_portfolio
            WHERE id = %s AND user_id = %s
        """, (tx_id, user_id))
        conn.commit()
        return {"status": "success", "message": f"Transaction {tx_id} deleted."}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cur.close()
        conn.close()

@router.get("/portfolio/export")
def export_portfolio_transactions(
    format: str = Query("csv", description="Export format: 'csv' or 'json'"),
    transaction_type: Optional[str] = Query(None, description="Filter by type: BUY, SELL, DIVIDEND, or ALL"),
    ticker: Optional[str] = Query(None, description="Filter by ticker symbol"),
    start_date: Optional[str] = Query(None, description="Filter orders from date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="Filter orders to date (YYYY-MM-DD)"),
    current_user: Optional[dict] = Depends(get_optional_current_user)
):
    """Exports portfolio buying and selling transaction history as a downloadable CSV or JSON file."""
    user_id = current_user["id"] if current_user else 1

    format_clean = format.lower().strip()
    if format_clean not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="Invalid format. Supported formats are 'csv' and 'json'.")

    query_parts = [
        f"""SELECT id, ticker, order_date, buy_price, quantity, transaction_type,
                  brokerage_fee, regulatory_fee, other_fee, source, created_at
           FROM {settings.mimir_schema}.mimir_portfolio
           WHERE user_id = %s AND (source IS NULL OR source = 'MANUAL' OR source = '')"""
    ]
    params = [user_id]

    if ticker:
        clean_ticker = ticker.strip().upper().lstrip('$')
        query_parts.append("AND UPPER(ticker) = %s")
        params.append(clean_ticker)

    if transaction_type and transaction_type.strip().upper() != "ALL":
        query_parts.append("AND UPPER(transaction_type) = %s")
        params.append(transaction_type.strip().upper())

    if start_date:
        query_parts.append("AND order_date >= %s")
        params.append(f"{start_date.strip()} 00:00:00")

    if end_date:
        query_parts.append("AND order_date <= %s")
        params.append(f"{end_date.strip()} 23:59:59")

    query_parts.append("ORDER BY order_date ASC, id ASC")
    full_sql = " ".join(query_parts)

    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        cur.execute(full_sql, tuple(params))
        rows = cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database query error: {str(e)}")
    finally:
        cur.close()
        conn.close()

    from datetime import timezone, timedelta
    gmt_plus_7 = timezone(timedelta(hours=7))

    records = []
    for r in rows:
        order_dt = r.get("order_date")
        if order_dt:
            if order_dt.tzinfo is None:
                order_dt_str = order_dt.strftime("%Y-%m-%d %H:%M:%S")
            else:
                order_dt_str = order_dt.astimezone(gmt_plus_7).strftime("%Y-%m-%d %H:%M:%S")
        else:
            order_dt_str = ""

        created_dt = r.get("created_at")
        if created_dt:
            if created_dt.tzinfo is None:
                created_dt_str = created_dt.strftime("%Y-%m-%d %H:%M:%S")
            else:
                created_dt_str = created_dt.astimezone(gmt_plus_7).strftime("%Y-%m-%d %H:%M:%S")
        else:
            created_dt_str = ""

        qty = float(r["quantity"]) if r.get("quantity") is not None else 0.0
        price = float(r["buy_price"]) if r.get("buy_price") is not None else 0.0
        b_fee = float(r.get("brokerage_fee") or 0.0)
        r_fee = float(r.get("regulatory_fee") or 0.0)
        o_fee = float(r.get("other_fee") or 0.0)
        total_fees = round(b_fee + r_fee + o_fee, 4)
        gross_total = round(qty * price, 4)
        ttype = (r.get("transaction_type") or "BUY").upper()

        if ttype == "BUY":
            net_total = round(gross_total + total_fees, 4)
        elif ttype in ("SELL", "DIVIDEND"):
            net_total = round(gross_total - total_fees, 4)
        else:
            net_total = round(gross_total, 4)

        records.append({
            "id": r["id"],
            "order_date": order_dt_str,
            "ticker": r["ticker"].upper(),
            "transaction_type": ttype,
            "quantity": qty,
            "price": price,
            "gross_total": gross_total,
            "brokerage_fee": b_fee,
            "regulatory_fee": r_fee,
            "other_fee": o_fee,
            "total_fees": total_fees,
            "net_total": net_total,
            "source": r.get("source") or "MANUAL",
            "created_at": created_dt_str
        })

    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")

    if format_clean == "json":
        json_content = json.dumps(records, indent=2)
        filename = f"mimir_portfolio_history_{now_str}.json"
        return Response(
            content=json_content,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    # Build CSV with UTF-8 BOM for Microsoft Excel compatibility
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow([
        "Transaction ID",
        "Order Date (GMT+7)",
        "Ticker",
        "Action",
        "Quantity",
        "Price ($)",
        "Gross Total ($)",
        "Brokerage Fee ($)",
        "Regulatory Fee ($)",
        "Other Fee ($)",
        "Total Fees ($)",
        "Net Total ($)",
        "Source",
        "Created At (GMT+7)"
    ])
    for rec in records:
        writer.writerow([
            rec["id"],
            rec["order_date"],
            rec["ticker"],
            rec["transaction_type"],
            rec["quantity"],
            rec["price"],
            rec["gross_total"],
            rec["brokerage_fee"],
            rec["regulatory_fee"],
            rec["other_fee"],
            rec["total_fees"],
            rec["net_total"],
            rec["source"],
            rec["created_at"]
        ])

    csv_content = output.getvalue()
    filename = f"mimir_portfolio_history_{now_str}.csv"
    return Response(
        content=csv_content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )



def fetch_online_stock_data(tickers: List[str]) -> Dict[str, List[Dict]]:
    if not tickers:
        return {}
    from curl_cffi.requests import Session
    session = Session(impersonate="chrome")
    session.verify = False
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    })
    
    def fetch_single_news(ticker):
        clean_ticker = ticker.strip().lstrip('$').upper()
        try:
            t = yf.Ticker(clean_ticker, session=session)
            news = t.news
            headlines = []
            if news:
                for n in news[:5]:
                    pub_ts = n.get("providerPublishTime")
                    recency = "Recent"
                    if pub_ts:
                        age_secs = int(time.time() - pub_ts)
                        if age_secs < 3600:
                            recency = f"{max(1, age_secs // 60)}m ago"
                        elif age_secs < 86400:
                            recency = f"{age_secs // 3600}h ago"
                        else:
                            recency = f"{age_secs // 86400}d ago"
                    headlines.append({
                        "title": n.get("title"),
                        "publisher": n.get("publisher", "Market Wire"),
                        "recency": recency
                    })
            return ticker, headlines
        except Exception:
            return ticker, []

    news_by_ticker = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        for tkr, items in executor.map(fetch_single_news, tickers):
            news_by_ticker[tkr] = items
    return news_by_ticker

def fetch_macro_indicators() -> Dict[str, Dict]:
    from curl_cffi.requests import Session
    session = Session(impersonate="chrome")
    session.verify = False
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    })
    
    macro_symbols = {
        "S&P 500": "^GSPC",
        "Nasdaq 100": "^NDX",
        "US Dollar Index": "DX-Y.NYB",
        "Gold": "GC=F",
        "US 10Y Yield": "^TNX",
        "Volatility (VIX)": "^VIX"
    }
    
    results = {}
    for name, sym in macro_symbols.items():
        try:
            t = yf.Ticker(sym, session=session)
            hist = t.history(period="5d")
            if not hist.empty:
                curr = float(hist["Close"].iloc[-1])
                prev = float(hist["Close"].iloc[-2]) if len(hist) > 1 else curr
                pct_change = ((curr - prev) / prev * 100) if prev > 0 else 0.0
                results[name] = {
                    "value": curr,
                    "change_pct": pct_change
                }
        except Exception:
            pass
    return results

def fetch_portfolio_price_trends(tickers: List[str]) -> Dict[str, Dict]:
    if not tickers:
        return {}
    trends = {}
    
    # 1. Fetch real-time live prices from mimir_hourly_ohlcv and MT5 bridge
    live_prices = fetch_current_prices(tickers)
    
    from curl_cffi.requests import Session
    session = Session(impersonate="chrome")
    session.verify = False
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    })

    # Bong Strats Quantitative Indicators
    try:
        from bong_strats.indicators import compute_volatility_regime, compute_ou_features
    except Exception:
        compute_volatility_regime = None
        compute_ou_features = None

    def fetch_single(t_symbol):
        try:
            clean_symbol = t_symbol.strip().lstrip('$').upper()
            live_p = live_prices.get(clean_symbol, 0.0) or live_prices.get(t_symbol, 0.0)
            t = yf.Ticker(clean_symbol, session=session)
            
            # Fetch 200 days of history for technical analysis
            hist = t.history(period="200d")
            techs = {
                "rsi_14": 50.0,
                "dma_50": 0.0,
                "dma_200": 0.0,
                "volume_ratio": 1.0,
                "price_trend_status": "Neutral",
                "support": 0.0,
                "resistance": 0.0,
                "atr_14": 0.0,
                "volatility_regime": "STABLE",
                "ou_zscore": 0.0,
                "wyckoff_seller_exhausted": False,
                "execution_bounds": {
                    "trigger_price": 0.0,
                    "stop_loss": 0.0,
                    "target_price": 0.0,
                    "risk_reward_ratio": 2.5,
                    "upside_pct": 0.0,
                    "downside_pct": 0.0
                }
            }
            
            current_price = float(live_p) if live_p > 0 else 0.0
            change_5d_pct = 0.0
            
            if not hist.empty:
                current_price = float(hist["Close"].iloc[-1])
                # Overlay real-time live tick price if fresh and available
                if live_p > 0:
                    current_price = float(live_p)
                    # Update latest bar close with the live tick price to ensure freshest technicals
                    hist.iloc[-1, hist.columns.get_loc("Close")] = current_price
                    if current_price > float(hist["High"].iloc[-1]):
                        hist.iloc[-1, hist.columns.get_loc("High")] = current_price
                    if current_price < float(hist["Low"].iloc[-1]):
                        hist.iloc[-1, hist.columns.get_loc("Low")] = current_price

                price_5d_ago = float(hist["Close"].iloc[-5]) if len(hist) >= 5 else float(hist["Close"].iloc[0])
                change_5d_pct = ((current_price - price_5d_ago) / price_5d_ago * 100) if price_5d_ago > 0 else 0.0
                
                # Compute Moving Averages
                closes = hist["Close"]
                volumes = hist["Volume"]
                highs = hist["High"]
                lows = hist["Low"]
                
                dma50 = float(closes.tail(50).mean()) if len(closes) >= 50 else current_price
                dma200 = float(closes.mean()) if len(closes) >= 200 else dma50
                
                # Compute RSI 14
                rsi14 = 50.0
                if len(closes) >= 15:
                    delta = closes.diff()
                    gain = delta.clip(lower=0)
                    loss = -delta.clip(upper=0)
                    avg_gain = gain.rolling(window=14).mean()
                    avg_loss = loss.rolling(window=14).mean()
                    rs = avg_gain / avg_loss
                    rsi_series = 100 - (100 / (1 + rs))
                    rsi14 = float(rsi_series.fillna(50.0).iloc[-1])
                    
                # Compute Volume Ratio (current vs 20-day average)
                vol_ratio = 1.0
                if len(volumes) >= 5:
                    avg_vol = float(volumes.tail(20).mean())
                    if avg_vol > 0:
                        vol_ratio = float(volumes.iloc[-1] / avg_vol)
                        
                # Determine price trend classification
                trend_status = "Neutral"
                if current_price > dma50 and dma50 > dma200:
                    trend_status = "Strongly Bullish (Golden Cross/Uptrend)"
                elif current_price > dma50:
                    trend_status = "Moderately Bullish (Above 50 DMA)"
                elif current_price < dma50 and dma50 < dma200:
                    trend_status = "Strongly Bearish (Death Cross/Downtrend)"
                elif current_price < dma50:
                    trend_status = "Moderately Bearish (Below 50 DMA)"

                # Support & Resistance
                support, resistance = find_support_resistance(highs, lows, closes)

                # 14-day ATR & War Rig Execution Bounds (1.5x ATR Stop, 3.0x ATR Target, R/R >= 2.5:1)
                tr1 = highs - lows
                tr2 = (highs - closes.shift(1)).abs()
                tr3 = (lows - closes.shift(1)).abs()
                tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                atr = float(tr.rolling(14).mean().iloc[-1]) if len(tr) >= 14 else current_price * 0.03
                if math.isnan(atr) or atr <= 0:
                    atr = current_price * 0.03

                sl_dist = max(0.038 * current_price, min(0.065 * current_price, 1.5 * atr))
                tp_dist = max(0.120 * current_price, min(0.250 * current_price, 3.0 * atr))
                tp_dist = max(tp_dist, sl_dist * 2.50)

                stop_loss = round(current_price - sl_dist, 2)
                target_price = round(current_price + tp_dist, 2)
                rr_ratio = round((target_price - current_price) / max(0.01, (current_price - stop_loss)), 2)
                upside_pct = round(((target_price / current_price) - 1.0) * 100.0, 1) if current_price > 0 else 0.0
                downside_pct = round((1.0 - (stop_loss / current_price)) * 100.0, 1) if current_price > 0 else 0.0

                # Volatility Regime
                vol_regime = "STABLE"
                if compute_volatility_regime is not None:
                    try:
                        df_norm = hist.rename(columns={'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'})
                        v_reg = compute_volatility_regime(df_norm)
                        vol_regime = str(v_reg.iloc[-1]) if not v_reg.empty else "STABLE"
                    except Exception:
                        vol_regime = "STABLE"
                else:
                    if len(closes) >= 20:
                        sma20 = closes.rolling(20).mean()
                        std20 = closes.rolling(20).std()
                        bw = (4 * std20) / sma20
                        bw_curr = float(bw.iloc[-1])
                        bw_avg = float(bw.rolling(50, min_periods=20).mean().iloc[-1]) if len(bw) >= 20 else bw_curr
                        if bw_curr < bw_avg * 0.75:
                            vol_regime = "SQUEEZE"
                        elif bw_curr > bw_avg * 1.40:
                            vol_regime = "EXHAUSTION"
                        elif bw_curr > bw_avg:
                            vol_regime = "EXPANDING"

                # Ornstein-Uhlenbeck SDE Mean-Reversion Z-score
                ou_z = 0.0
                if compute_ou_features is not None:
                    try:
                        df_norm = hist.rename(columns={'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'})
                        ou_df = compute_ou_features(df_norm, lookback=min(40, len(df_norm) - 1))
                        ou_z = float(ou_df["ou_zscore"].iloc[-1]) if pd.notna(ou_df["ou_zscore"].iloc[-1]) else 0.0
                    except Exception:
                        ou_z = 0.0

                # Wyckoff Seller Absorption
                seller_exhausted = False
                try:
                    if len(hist) >= 22:
                        prior_vol = volumes.shift(1).rolling(20, min_periods=10).mean()
                        prev_bar = hist.iloc[-2]
                        curr_bar = hist.iloc[-1]
                        prev_avg = prior_vol.iloc[-2]
                        is_vol_surge = (pd.notna(prev_avg) and prev_avg > 0 and float(prev_bar["Volume"]) >= prev_avg * 1.5)
                        bearish_surge = is_vol_surge and (float(prev_bar["Close"]) < float(prev_bar["Open"]))
                        seller_exhausted = bool(bearish_surge and (float(curr_bar["Low"]) >= float(prev_bar["Low"])) and (float(curr_bar["Close"]) > float(curr_bar["Open"])))
                except Exception:
                    pass
                    
                techs = {
                    "rsi_14": round(rsi14, 2),
                    "dma_50": round(dma50, 2),
                    "dma_200": round(dma200, 2),
                    "volume_ratio": round(vol_ratio, 2),
                    "price_trend_status": trend_status,
                    "support": round(support, 2),
                    "resistance": round(resistance, 2),
                    "atr_14": round(atr, 2),
                    "volatility_regime": vol_regime,
                    "ou_zscore": round(ou_z, 2),
                    "wyckoff_seller_exhausted": seller_exhausted,
                    "execution_bounds": {
                        "trigger_price": current_price,
                        "stop_loss": stop_loss,
                        "target_price": target_price,
                        "risk_reward_ratio": rr_ratio,
                        "upside_pct": upside_pct,
                        "downside_pct": downside_pct
                    }
                }
            
            # Fetch fundamental details
            info = {}
            try:
                raw_info = t.info
                if raw_info:
                    info = {
                        "pe_ratio": raw_info.get("trailingPE"),
                        "forward_pe": raw_info.get("forwardPE"),
                        "peg_ratio": raw_info.get("pegRatio"),
                        "pb_ratio": raw_info.get("priceToBook"),
                        "debt_to_equity": raw_info.get("debtToEquity"),
                        "dividend_yield": raw_info.get("dividendYield"),
                        "profit_margin": raw_info.get("profitMargins"),
                        "market_cap": raw_info.get("marketCap")
                    }
            except Exception:
                pass
                
            return t_symbol, {
                "current_price": current_price,
                "change_5d_pct": change_5d_pct,
                "technical_analysis": techs,
                "fundamental_analysis": info
            }
        except Exception:
            pass
        return t_symbol, None

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = executor.map(fetch_single, tickers)
        for t_symbol, data in results:
            if data is not None:
                trends[t_symbol] = data
            else:
                trends[t_symbol] = {
                    "current_price": 0.0,
                    "change_5d_pct": 0.0,
                    "technical_analysis": {
                        "rsi_14": 50.0,
                        "dma_50": 0.0,
                        "dma_200": 0.0,
                        "volume_ratio": 1.0,
                        "price_trend_status": "Neutral",
                        "support": 0.0,
                        "resistance": 0.0,
                        "atr_14": 0.0,
                        "volatility_regime": "STABLE",
                        "ou_zscore": 0.0,
                        "wyckoff_seller_exhausted": False,
                        "execution_bounds": {
                            "trigger_price": 0.0,
                            "stop_loss": 0.0,
                            "target_price": 0.0,
                            "risk_reward_ratio": 2.5,
                            "upside_pct": 0.0,
                            "downside_pct": 0.0
                        }
                    },
                    "fundamental_analysis": {}
                }
                
    return trends

@router.get("/portfolio/advice")
def get_portfolio_advice(current_user: Optional[dict] = Depends(get_optional_current_user)):
    user_id = current_user["id"] if current_user else 1

    # 1. Fetch current portfolio
    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    cur.execute(f"""
        SELECT ticker, buy_price, quantity, transaction_type, order_date
        FROM {settings.mimir_schema}.mimir_portfolio
        WHERE user_id = %s
    """, (user_id,))
    txs = cur.fetchall()
    
    if not txs:
        cur.close()
        conn.close()
        return {
            "advice": "<div class='p-6 text-center text-[#8BA4A8] font-mono'>Add active transactions to your portfolio to activate the War Rig AI Sentinel strategic diagnostic!</div>"
        }
        
    # Group transactions by ticker
    raw_holdings = {}
    for tx in txs:
        ticker = tx["ticker"].upper()
        if ticker not in raw_holdings:
            raw_holdings[ticker] = []
        raw_holdings[ticker].append(tx)
        
    portfolio_list = []
    for ticker, txs_list in raw_holdings.items():
        txs_sorted = sorted(txs_list, key=lambda x: x["order_date"])
        
        qty_sum = 0.0
        avg_buy = 0.0
        realized_pl = 0.0
        
        for tx in txs_sorted:
            tx_qty = float(tx["quantity"])
            tx_price = float(tx["buy_price"])
            tx_type = tx.get("transaction_type", "BUY").upper()
            
            if tx_type == "BUY":
                if qty_sum + tx_qty > 0:
                    avg_buy = (qty_sum * avg_buy + tx_qty * tx_price) / (qty_sum + tx_qty)
                else:
                    avg_buy = 0.0
                qty_sum += tx_qty
            elif tx_type == "SELL":
                realized_pl += tx_qty * (tx_price - avg_buy)
                qty_sum -= tx_qty
            
            qty_sum = round(qty_sum, 8)
            if qty_sum <= 0.00001:
                qty_sum = 0.0
                avg_buy = 0.0
        if qty_sum > 0.00001:
            portfolio_list.append({
                "ticker": ticker,
                "quantity": qty_sum,
                "avg_price": avg_buy,
                "realized_pl": realized_pl
            })
            
    tickers_list = [p["ticker"] for p in portfolio_list]
    tickers_tuple = tuple(tickers_list)
    
    # 2. Get recent sentiment impacts and detailed NLP reasoning for the user's stocks
    sentiment_data = []
    if tickers_tuple:
        if len(tickers_tuple) == 1:
            query = f"""
                SELECT si.ticker, AVG(si.sentiment_score) as avg_score, COUNT(DISTINCT a.id) as article_count,
                       json_agg(json_build_object(
                           'title', a.title,
                           'source', a.source_name,
                           'published_ts', to_char(a.published_ts, 'YYYY-MM-DD HH24:MI'),
                           'age_hours', round((EXTRACT(EPOCH FROM (NOW() - a.published_ts))/3600.0)::numeric, 1),
                           'reasoning', si.reasoning,
                           'score', round(si.sentiment_score::numeric, 2),
                           'direction', si.direction,
                           'is_spillover', COALESCE(si.is_spillover, false),
                           'spillover_source_asset', si.spillover_source_asset
                       ) ORDER BY a.published_ts DESC) as articles
                FROM {settings.mimir_schema}.mimir_sentiment_impacts si
                JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
                WHERE si.ticker = %s AND a.published_ts > NOW() - INTERVAL '14 days'
                GROUP BY si.ticker
            """
            cur.execute(query, (tickers_tuple[0],))
        else:
            query = f"""
                SELECT si.ticker, AVG(si.sentiment_score) as avg_score, COUNT(DISTINCT a.id) as article_count,
                       json_agg(json_build_object(
                           'title', a.title,
                           'source', a.source_name,
                           'published_ts', to_char(a.published_ts, 'YYYY-MM-DD HH24:MI'),
                           'age_hours', round((EXTRACT(EPOCH FROM (NOW() - a.published_ts))/3600.0)::numeric, 1),
                           'reasoning', si.reasoning,
                           'score', round(si.sentiment_score::numeric, 2),
                           'direction', si.direction,
                           'is_spillover', COALESCE(si.is_spillover, false),
                           'spillover_source_asset', si.spillover_source_asset
                       ) ORDER BY a.published_ts DESC) as articles
                FROM {settings.mimir_schema}.mimir_sentiment_impacts si
                JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
                WHERE si.ticker IN %s AND a.published_ts > NOW() - INTERVAL '14 days'
                GROUP BY si.ticker
            """
            cur.execute(query, (tickers_tuple,))
        sentiment_data = cur.fetchall()

    # 3. Upcoming earnings calendar lookup
    earnings_calendar_map = {}
    if tickers_list:
        try:
            cur.execute(f"""
                SELECT ticker, company_name, earnings_date, earnings_time, estimated_eps
                FROM {settings.mimir_schema}.mimir_earnings_calendar
                WHERE ticker = ANY(%s) AND earnings_date >= CURRENT_DATE
                ORDER BY earnings_date ASC
            """, (tickers_list,))
            for er in cur.fetchall():
                t_k = er["ticker"].upper()
                if t_k not in earnings_calendar_map:
                    edate = er["earnings_date"]
                    days_until = (edate - datetime.now(timezone.utc).date()).days if hasattr(edate, "strftime") else None
                    earnings_calendar_map[t_k] = {
                        "company_name": er["company_name"],
                        "earnings_date": edate.strftime('%Y-%m-%d') if hasattr(edate, "strftime") else str(edate),
                        "days_until": days_until,
                        "earnings_time": er["earnings_time"] or "N/A",
                        "estimated_eps": float(er["estimated_eps"]) if er["estimated_eps"] is not None else None
                    }
        except Exception as ec_err:
            print(f"[PORTFOLIO EARNINGS ERROR] {ec_err}")

    # 4. Fetch live War Rig Convergence signals from alpha pipeline
    war_rig_signals = []
    try:
        cur.execute(f"""
            SELECT ticker, signal_type, trigger_price, target_price, stop_loss, conviction_score,
                   catalyst_type, holding_period, investment_thesis, headline, reason, created_at
            FROM {settings.mimir_schema}.mimir_trade_signals
            WHERE catalyst_type = 'WAR_RIG_CONVERGENCE'
            ORDER BY created_at DESC
            LIMIT 5
        """)
        for s_row in cur.fetchall():
            trig = float(s_row["trigger_price"]) if s_row["trigger_price"] is not None else 0.0
            targ = float(s_row["target_price"]) if s_row["target_price"] is not None else 0.0
            sl = float(s_row["stop_loss"]) if s_row["stop_loss"] is not None else 0.0
            rr = round((targ - trig) / max(0.01, (trig - sl)), 2) if (trig - sl) > 0 else 2.5
            conv = float(s_row["conviction_score"]) if s_row["conviction_score"] is not None else 0.75
            war_rig_signals.append({
                "ticker": s_row["ticker"],
                "signal_type": s_row["signal_type"],
                "trigger_price": trig,
                "target_price": targ,
                "stop_loss": sl,
                "risk_reward_ratio": rr,
                "conviction_score": round(conv if conv <= 1.0 else conv / 100.0, 2),
                "conviction_pct": round(conv * 100 if conv <= 1.0 else conv, 0),
                "headline": s_row["headline"],
                "holding_period": s_row["holding_period"],
                "investment_thesis": s_row["investment_thesis"]
            })
    except Exception as wr_err:
        print(f"[PORTFOLIO WAR RIG SIGNALS ERROR] {wr_err}")

    # 5. Sector Tailwinds for user's tickers
    sector_tailwinds = {}
    for ticker in tickers_list:
        try:
            st = get_ticker_sector_tailwinds(ticker, conn=conn)
            sector_tailwinds[ticker] = {
                "sector_name": st.get("sector_name", "General Market"),
                "sector_phase": st.get("phase", "CONSOLIDATION"),
                "rs_5d_vs_spy": float(st.get("rs_5d", 0.0)),
                "sector_thesis": st.get("sector_thesis", "")
            }
        except Exception:
            sector_tailwinds[ticker] = {
                "sector_name": "General Market",
                "sector_phase": "CONSOLIDATION",
                "rs_5d_vs_spy": 0.0,
                "sector_thesis": "Sector tracking neutral."
            }
        
    # 6. Get top positive sentiment equities in the last 7 days as candidates for stock picks
    query_picks = f"""
        SELECT si.ticker, si.asset_name, AVG(si.sentiment_score) as score, COUNT(DISTINCT a.id) as count,
               json_agg(json_build_object('title', a.title, 'reasoning', si.reasoning, 'source', a.source_name) ORDER BY a.published_ts DESC) as articles
        FROM {settings.mimir_schema}.mimir_sentiment_impacts si
        JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
        WHERE si.ticker IS NOT NULL AND si.asset_category = 'EQUITY'
          AND a.published_ts > NOW() - INTERVAL '7 days'
        GROUP BY si.ticker, si.asset_name
        HAVING AVG(si.sentiment_score) > 0.15 AND COUNT(DISTINCT a.id) >= 1
        ORDER BY score DESC, count DESC
        LIMIT 5
    """
    cur.execute(query_picks)
    picks = cur.fetchall()
    
    cur.close()
    conn.close()
    
    # 7. Real-time online data, macro indicators, and technical price trends
    online_news = fetch_online_stock_data(tickers_list)
    macro_trends = fetch_macro_indicators()
    price_trends = fetch_portfolio_price_trends(tickers_list)
    
    # Enrich portfolio_list with profit, price movement, technical indicators, fundamental metrics, sector, and bounds
    enriched_portfolio = []
    for p in portfolio_list:
        ticker = p["ticker"]
        qty = p["quantity"]
        avg_price = p["avg_price"]
        realized_pl = p["realized_pl"]
        
        trend = price_trends.get(ticker, {
            "current_price": 0.0,
            "change_5d_pct": 0.0,
            "technical_analysis": {
                "rsi_14": 50.0,
                "dma_50": 0.0,
                "dma_200": 0.0,
                "volume_ratio": 1.0,
                "price_trend_status": "Neutral",
                "support": 0.0,
                "resistance": 0.0,
                "atr_14": 0.0,
                "volatility_regime": "STABLE",
                "ou_zscore": 0.0,
                "wyckoff_seller_exhausted": False,
                "execution_bounds": {
                    "trigger_price": 0.0,
                    "stop_loss": 0.0,
                    "target_price": 0.0,
                    "risk_reward_ratio": 2.5,
                    "upside_pct": 0.0,
                    "downside_pct": 0.0
                }
            },
            "fundamental_analysis": {}
        })
        current_price = trend["current_price"]
        if current_price == 0.0:
            current_price = avg_price
            
        cost_basis = qty * avg_price
        current_val = qty * current_price
        unrealized_pl = current_val - cost_basis
        unrealized_pl_pct = (unrealized_pl / cost_basis * 100) if cost_basis > 0 else 0.0
        
        enriched_portfolio.append({
            "ticker": ticker,
            "quantity": qty,
            "avg_buy_price": avg_price,
            "current_price": current_price,
            "cost_basis": cost_basis,
            "current_value": current_val,
            "unrealized_profit_loss": unrealized_pl,
            "unrealized_profit_loss_pct": unrealized_pl_pct,
            "realized_profit_loss": realized_pl,
            "total_profit_loss": unrealized_pl + realized_pl,
            "recent_price_movement": {
                "current_price": current_price,
                "change_5d_pct": trend["change_5d_pct"]
            },
            "technical_analysis": trend.get("technical_analysis", {}),
            "fundamental_analysis": trend.get("fundamental_analysis", {}),
            "sector_context": sector_tailwinds.get(ticker, {}),
            "earnings_calendar": earnings_calendar_map.get(ticker, None),
            "asymmetric_execution_bounds": trend.get("technical_analysis", {}).get("execution_bounds", {})
        })
        
    # Format top sentiment picks with catalyst headlines
    formatted_picks = []
    for pk in picks:
        top_art = pk["articles"][0] if pk.get("articles") else {}
        formatted_picks.append({
            "ticker": pk["ticker"],
            "name": pk["asset_name"],
            "sentiment_score": float(pk["score"]),
            "mentions": pk["count"],
            "catalyst_headline": top_art.get("title", "Bullish institutional accumulation"),
            "catalyst_reasoning": top_art.get("reasoning", "Positive fundamental sentiment momentum"),
            "catalyst_source": top_art.get("source", "MIMIR Scraper"),
            "catalyst_age_hours": top_art.get("age_hours")
        })

    # Format portfolio sentiment detail
    portfolio_sentiment_detail = []
    for s in sentiment_data:
        articles_formatted = []
        for art in (s["articles"] or [])[:4]:
            articles_formatted.append({
                "title": art.get("title"),
                "source": art.get("source"),
                "published_ts": art.get("published_ts"),
                "age_hours": art.get("age_hours"),
                "reasoning": art.get("reasoning"),
                "sentiment_score": art.get("score"),
                "direction": art.get("direction"),
                "is_spillover": art.get("is_spillover", False),
                "spillover_source_asset": art.get("spillover_source_asset")
            })
        portfolio_sentiment_detail.append({
            "ticker": s["ticker"],
            "avg_sentiment_score": round(float(s["avg_score"]), 2),
            "article_count": s["article_count"],
            "recent_articles": articles_formatted
        })

    # 8. Formulate Unified Prompt Context with Live Snapshot Timestamp
    snapshot_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    prompt_context = {
        "pipeline_snapshot_timestamp": snapshot_time,
        "portfolio_holdings": enriched_portfolio,
        "portfolio_sentiment_and_catalysts": portfolio_sentiment_detail,
        "macro_regime_and_indicators": macro_trends,
        "live_war_rig_convergence_signals": war_rig_signals,
        "top_sentiment_and_catalyst_picks": formatted_picks,
        "online_realtime_news": online_news
    }
    
    system_prompt = (
        "You are MIMIR's Chief Investment Officer and Lead Quantitative Strategist for the War Rig Alpha Transmission Engine. "
        "Your task is to conduct an uncompromising, institutional-grade portfolio diagnostic and strategic briefing. "
        "You synthesize quantitative microstructure (Cylinder 3), high-impact catalyst provenance & NLP news reasoning (Cylinder 2), "
        "and SPDR macro sector flow transmission (Cylinder 1) into sharp, actionable strategic advice. "
        "Write in an authoritative, hedge-fund partner tone. Avoid introductory remarks, generic disclaimers, or conversational fluff. "
        "STRICT GROUNDING DIRECTIVE: Rely EXCLUSIVELY on the explicit metrics, execution bounds, news reasonings, and indicators "
        "provided in the CONTEXT DATA JSON. Do NOT invent, hallucinate, or assume metrics. If an indicator is missing, mark it 'N/A'."
    )
    
    user_prompt = f"""
You are provided with the following real-time and historical context from the MIMIR War Rig Pipeline:
---
CONTEXT DATA:
{json.dumps(prompt_context, indent=2)}
---

Generate a comprehensive strategic briefing for the user's portfolio. The output MUST be raw HTML fragments (do not wrap in ```html or other markdown blocks; do NOT include <style>, <script>, <html>, <head>, or <body> tags; just start with the HTML elements directly).
Use Tailwind CSS classes to style the output so it looks premium, sleek, and matches a high-end terminal dashboard (dark theme, using the application's palette of dark slate, emerald, cyan, amber, gold, and red/coral).

STRICT HTML & STYLING DIRECTIVES:
- Do NOT include <style> blocks, CSS definitions, or custom CSS rules under any circumstances.
- Do NOT include <script> tags, external links, or stylesheet links.
- Format all numeric values cleanly (dollar amounts prefixed with $, percentages to 1 or 2 decimals).

STRICT DATA INTEGRITY & FRESHNESS DIRECTIVES:
- Do NOT search online, use pre-trained general knowledge, or fabricate numbers.
- Ground your tactical directives in the freshest market data provided (note `pipeline_snapshot_timestamp`, article `age_hours`, and online headline `recency`). Prioritize breaking news developments and active intraday price momentum over stale narrative trends.
- You MUST cite specific news headlines and catalyst reasoning from `portfolio_sentiment_and_catalysts` instead of vague sentiment scores.
- You MUST utilize the exact War Rig execution bounds (`stop_loss`, `target_price`, `risk_reward_ratio`), technical metrics (`rsi_14`, `volume_ratio`, `volatility_regime`, `ou_zscore`), and sector context (`sector_name`, `sector_phase`, `rs_5d_vs_spy`) provided in each position.
- Do NOT include any section titled "Alternative MIMIR Profit Strategies". That legacy section is permanently decommissioned.

Ensure the HTML includes EXACTLY the following 4 sections, designed beautifully:

1. <div class="mb-8">
     <div class="flex items-center justify-between border-b border-[#1A2A30] pb-2 mb-4">
       <h3 class="text-lg font-bold text-[#00E5F2] flex items-center gap-2">
         <span>🛡️</span> Portfolio Health & 3-Cylinder Diagnostic Matrix
       </h3>
       <span class="text-xs font-mono text-[#8BA4A8]">Unified War Rig Single-Shaft Analysis</span>
     </div>
     <p class="text-[#8BA4A8] text-xs mb-4 leading-relaxed">
       Comprehensive institutional assessment correlating portfolio concentration, sector rotation phases, microstructure asymmetry, and impending catalyst risk. Positions are stress-tested against War Rig Cylinder 1 (Sector Inflows), Cylinder 2 (Catalysts & Earnings), and Cylinder 3 (Microstructure Bounds).
     </p>
     <div class="overflow-x-auto mb-4 border border-[#1A2A30] rounded bg-[#0A0E12]/80">
       <table class="w-full text-left text-xs text-[#D6E5E3] border-collapse">
         <thead>
           <tr class="border-b border-[#1A2A30] bg-[#111C21] text-[#8BA4A8] font-mono uppercase text-[11px]">
             <th class="py-2.5 px-3">Position</th>
             <th class="py-2.5 px-3">Cylinder 1: Sector / RS</th>
             <th class="py-2.5 px-3">Cylinder 2: Catalyst / News</th>
             <th class="py-2.5 px-3">Cylinder 3: Microstructure</th>
             <th class="py-2.5 px-3">War Rig Bounds</th>
             <th class="py-2.5 px-3">Action</th>
             <th class="py-2.5 px-3">Institutional Rationale</th>
           </tr>
         </thead>
         <tbody class="divide-y divide-[#1A2A30]">
           <!-- Generate rows for each position in portfolio_holdings -->
         </tbody>
       </table>
     </div>
   </div>

For each position's table row:
- Position: Display Ticker bold, Market Value, Cost Basis, and Unrealized P&L formatted with green/red coloring.
- Cylinder 1: Display Sector Name and Sector Phase badge (e.g. STEALTH ACCUMULATION, MARKUP, ACCUMULATION, DISTRIBUTION, CONSOLIDATION) with 5D RS vs SPY.
- Cylinder 2: Cite specific headline or NLP reasoning from `portfolio_sentiment_and_catalysts`. If upcoming earnings exists in `earnings_calendar`, display an amber badge: `Earnings in Xd (BMO/AMC)`.
- Cylinder 3: Display RSI momentum, Volume ratio, Volatility Regime badge (SQUEEZE, EXPANDING, EXHAUSTION, STABLE), and OU Z-score.
- War Rig Bounds: Display Invalidation Stop Loss, Target Price, and Risk/Reward ratio (e.g., `Stop: $X | Target: $Y (R/R: Z:1)`).
- Action Column Badge: Choose strictly one of:
  * <span class="bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 px-2 py-0.5 rounded text-[11px] font-bold font-mono">CONVICTION BUY</span>
  * <span class="bg-cyan-500/10 text-cyan-400 border border-cyan-500/30 px-2 py-0.5 rounded text-[11px] font-bold font-mono">ACCUMULATE</span>
  * <span class="bg-amber-500/10 text-amber-400 border border-amber-500/30 px-2 py-0.5 rounded text-[11px] font-bold font-mono">HOLD RUNNER</span>
  * <span class="bg-orange-500/10 text-orange-400 border border-orange-500/30 px-2 py-0.5 rounded text-[11px] font-bold font-mono">TRIM PROFIT</span>
  * <span class="bg-red-500/10 text-red-400 border border-red-500/30 px-2 py-0.5 rounded text-[11px] font-bold font-mono">EXIT INVALIDATED</span>
- Institutional Rationale: A sharp 1-2 sentence institutional synthesis explicitly combining Technicals + Fundamentals + News Catalyst + Sector Phase.

2. <div class="mb-8">
     <div class="flex items-center justify-between border-b border-[#1A2A30] pb-2 mb-4">
       <h3 class="text-lg font-bold text-[#FFD700] flex items-center gap-2">
         <span>⚔️</span> War Rig Convergence Alpha Opportunities
       </h3>
       <span class="text-xs font-mono text-[#8BA4A8]">Institutional Crankshaft Inflows (Conviction >= 75%)</span>
     </div>
     <p class="text-[#8BA4A8] text-xs mb-4 leading-relaxed">
       Active convergence setups generated by MIMIR's automated multi-factor pipeline. These opportunities satisfy Cylinder 1 (Sector Tailwinds), Cylinder 2 (Pre-Earnings Beat or Supply Chain Contagion), and Cylinder 3 (Hard Asymmetry Gate >= 2.5:1 R/R).
     </p>
     <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
       <!-- Generate card for 2-3 top candidates from live_war_rig_convergence_signals or top_sentiment_and_catalyst_picks -->
     </div>
   </div>
   Format each opportunity card with:
   - Header with Ticker, Conviction Score badge (e.g. 85% Conviction), and R/R ratio.
   - 3-Cylinder Convergence breakdown: Sector Flow, Catalyst Trigger with headline/reasoning, and Technical Execution Bounds.
   - Nitrous Express Options Pod recommendation (e.g., Mode A Vertical Bull Call Spread or Mode B Credit Bull Put Spread if IV is elevated).

3. <div class="mb-8">
     <div class="flex items-center justify-between border-b border-[#1A2A30] pb-2 mb-4">
       <h3 class="text-lg font-bold text-[#00E676] flex items-center gap-2">
         <span>🌍</span> Macro Regime & Sector Transmission
       </h3>
       <span class="text-xs font-mono text-[#8BA4A8]">Global Multi-Asset Context</span>
     </div>
     <p class="text-[#8BA4A8] text-xs mb-3 leading-relaxed">
       Synthesize the global indicators from `macro_regime_and_indicators` (S&P 500, Nasdaq 100, VIX, US Dollar Index, Gold, US 10Y Yield). Identify the prevailing macroeconomic regime (e.g. Risk-On Expansion, Hawkish Liquidity Drag, Dollar Flight, Stagflationary Squeeze) and directly evaluate its balance-sheet and multiple-compression impacts on the user's specific sector holdings.
     </p>
   </div>

4. <div class="mb-6">
     <div class="flex items-center justify-between border-b border-[#1A2A30] pb-2 mb-4">
       <h3 class="text-lg font-bold text-[#00A6B2] flex items-center gap-2">
         <span>🎯</span> Tactical Execution Deck & Asymmetric Rebalancing
       </h3>
       <span class="text-xs font-mono text-[#8BA4A8]">Actionable Capital Allocation Directives</span>
     </div>
     <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
       <div class="p-4 rounded border border-[#1A2A30] bg-[#0A0E12]/60">
         <h4 class="text-xs font-bold font-mono uppercase text-[#FF5252] mb-2 flex items-center gap-1.5">
           <i class="fas fa-shield-alt"></i> Immediate Invalidation Stops & Trims
         </h4>
         <p class="text-xs text-[#D6E5E3] leading-relaxed">
           Specify exact portfolio holdings approaching or violating ATR stop-loss bounds, or trapped in distribution sectors, that require immediate trimming or stop protection.
         </p>
       </div>
       <div class="p-4 rounded border border-[#1A2A30] bg-[#0A0E12]/60">
         <h4 class="text-xs font-bold font-mono uppercase text-[#FFB300] mb-2 flex items-center gap-1.5">
           <i class="fas fa-calendar-check"></i> Catalyst & Earnings Danger Radar
         </h4>
         <p class="text-xs text-[#D6E5E3] leading-relaxed">
           Highlight upcoming earnings reports (2-14 days out) or major macro releases for held assets, specifying whether to hold runners into the print or de-risk ahead of implied volatility crush.
         </p>
       </div>
       <div class="p-4 rounded border border-[#1A2A30] bg-[#0A0E12]/60">
         <h4 class="text-xs font-bold font-mono uppercase text-[#00E5F2] mb-2 flex items-center gap-1.5">
           <i class="fas fa-sync-alt"></i> Capital Rebalancing Directives
         </h4>
         <p class="text-xs text-[#D6E5E3] leading-relaxed">
           Provide concrete capital rotation orders: reallocating cash from lagger/distribution names into top War Rig accumulation sectors.
         </p>
       </div>
       <div class="p-4 rounded border border-[#1A2A30] bg-[#0A0E12]/60">
         <h4 class="text-xs font-bold font-mono uppercase text-[#00E676] mb-2 flex items-center gap-1.5">
           <i class="fas fa-layer-group"></i> Nitrous Convexity & Options Hedging
         </h4>
         <p class="text-xs text-[#D6E5E3] leading-relaxed">
           Tactical options guidance to hedge downside exposure or capture asymmetric upside on core positions with strictly capped capital risk.
         </p>
       </div>
     </div>
   </div>

Do NOT include any generic concluding remarks, disclaimers, or conversational filler. Start directly with the first section's HTML wrapper.
"""

    # Call LLM via centralized completion router
    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        content = send_chat_completion(
            messages=messages,
            temperature=0.3,
            timeout=75
        )
        
        # Clean markdown code block wraps and sanitize HTML content
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r'^```[a-zA-Z]*\s*', '', content)
        if content.endswith("```"):
            content = re.sub(r'\s*```$', '', content)
            
        # Strip any <style>...</style>, <script>...</script>, and global wrapper tags to prevent document style leaks
        content = re.sub(r'<style[^>]*>[\s\S]*?</style>', '', content, flags=re.IGNORECASE)
        content = re.sub(r'<script[^>]*>[\s\S]*?</script>', '', content, flags=re.IGNORECASE)
        content = re.sub(r'<head[^>]*>[\s\S]*?</head>', '', content, flags=re.IGNORECASE)
        content = re.sub(r'<!DOCTYPE[^>]*>', '', content, flags=re.IGNORECASE)
        content = re.sub(r'</?(?:html|head|body)[^>]*>', '', content, flags=re.IGNORECASE)
        content = re.sub(r'<link[^>]*rel=["\']stylesheet["\'][^>]*>', '', content, flags=re.IGNORECASE)
        return {
            "advice": content,
            "generated_at": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        return {
            "advice": f"<p class='text-[#FF5252] font-mono text-xs'>Error generating War Rig AI advice: {str(e)}. Please try again later.</p>"
        }


@router.get("/portfolio/history")
def get_portfolio_history(
    period: str = Query("1m", description="1w, 1m, 3m, 6m, 1y, ytd, all"),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    benchmark: str = Query("SPY", description="Benchmark ticker to compare against"),
    current_user: Optional[dict] = Depends(get_optional_current_user)
):
    user_id = current_user["id"] if current_user else 1

    from datetime import timedelta, date, datetime as dt
    import time
    import math
    import numpy as np
    from curl_cffi.requests import Session

    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    # 1. Fetch all transactions sorted by order_date
    cur.execute(f"""
        SELECT ticker, buy_price, quantity, transaction_type, order_date,
               COALESCE(brokerage_fee, 0.0) as brokerage_fee,
               COALESCE(regulatory_fee, 0.0) as regulatory_fee,
               COALESCE(other_fee, 0.0) as other_fee
        FROM {settings.mimir_schema}.mimir_portfolio
        WHERE user_id = %s
        ORDER BY order_date ASC
    """, (user_id,))
    txs = cur.fetchall()
    
    if not txs:
        cur.close()
        conn.close()
        return {
            "history": [],
            "benchmark": [],
            "metrics": {},
            "allocation": [],
            "monthly_matrix": []
        }
        
    tickers = list(set(tx["ticker"].upper() for tx in txs))
    bm_ticker = benchmark.upper()
    all_tickers_to_fetch = list(set(tickers + [bm_ticker]))
    
    # Calculate date range
    now_utc = datetime.now(timezone.utc)
    
    if start_date and end_date:
        try:
            calc_start_date = dt.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            calc_end_date = dt.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            calc_end_date = now_utc
            calc_start_date = now_utc - timedelta(days=30)
    else:
        calc_end_date = now_utc
        p = period.lower()
        if p == "1w":
            calc_start_date = now_utc - timedelta(days=7)
        elif p == "1m":
            calc_start_date = now_utc - timedelta(days=30)
        elif p == "3m":
            calc_start_date = now_utc - timedelta(days=90)
        elif p == "6m":
            calc_start_date = now_utc - timedelta(days=180)
        elif p == "1y":
            calc_start_date = now_utc - timedelta(days=365)
        elif p == "ytd":
            calc_start_date = dt(now_utc.year, 1, 1, tzinfo=timezone.utc)
        elif p == "all":
            earliest_tx = txs[0]["order_date"]
            if isinstance(earliest_tx, dt):
                calc_start_date = earliest_tx if earliest_tx.tzinfo else earliest_tx.replace(tzinfo=timezone.utc)
            else:
                calc_start_date = now_utc - timedelta(days=365)
        else:
            calc_start_date = now_utc - timedelta(days=30)
            
    if calc_start_date > calc_end_date:
        calc_start_date, calc_end_date = calc_end_date - timedelta(days=30), calc_end_date

    days_diff = max(1, (calc_end_date.date() - calc_start_date.date()).days)
    
    prices_map = {} # {ticker: {date_str: price}}
    
    session = Session(impersonate="chrome")
    session.verify = False
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    })
    
    def fetch_ticker_history(ticker):
        ticker_prices = {}
        try:
            cur_p = conn.cursor()
            cur_p.execute(f"""
                SELECT DISTINCT ON (DATE(timestamp)) DATE(timestamp) as date, close
                FROM {settings.mimir_schema}.mimir_hourly_ohlcv
                WHERE ticker = %s AND timestamp >= %s - INTERVAL '15 days'
                ORDER BY DATE(timestamp), timestamp DESC
            """, (ticker, calc_start_date))
            rows = cur_p.fetchall()
            cur_p.close()
            
            for row in rows:
                date_str = row["date"].strftime("%Y-%m-%d")
                ticker_prices[date_str] = float(row["close"])
        except Exception as e:
            print(f"[PORTFOLIO HISTORY] DB error for {ticker}: {e}")
            
        # yfinance fallback
        yf_period = "1mo"
        if days_diff > 300:
            yf_period = "max"
        elif days_diff > 150:
            yf_period = "1y"
        elif days_diff > 60:
            yf_period = "6mo"
        elif days_diff > 14:
            yf_period = "3mo"
            
        if len(ticker_prices) < max(5, days_diff * 0.4):
            try:
                t = yf.Ticker(ticker, session=session)
                df = t.history(period=yf_period, interval="1d")
                for index, row in df.iterrows():
                    date_str = index.strftime("%Y-%m-%d")
                    ticker_prices[date_str] = float(row["Close"])
            except Exception as e:
                print(f"[PORTFOLIO HISTORY] yfinance error for {ticker}: {e}")
                
        return ticker, ticker_prices

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = executor.map(fetch_ticker_history, all_tickers_to_fetch)
        for ticker, t_prices in results:
            prices_map[ticker] = t_prices
            
    cur.close()
    conn.close()
    
    # Generate list of dates from calc_start_date to calc_end_date
    date_list = []
    num_days = (calc_end_date.date() - calc_start_date.date()).days + 1
    for i in range(num_days):
        day = calc_start_date + timedelta(days=i)
        date_list.append(day.date())
        
    history_data = []
    total_brokerage_fees = 0.0
    total_regulatory_fees = 0.0
    total_other_fees = 0.0
    total_dividends_earned = 0.0
    
    # Calculate fee totals from txs
    for tx in txs:
        total_brokerage_fees += float(tx.get("brokerage_fee") or 0)
        total_regulatory_fees += float(tx.get("regulatory_fee") or 0)
        total_other_fees += float(tx.get("other_fee") or 0)
        if tx.get("transaction_type", "").upper() == "DIVIDEND":
            total_dividends_earned += float(tx.get("quantity") or 0) * float(tx.get("buy_price") or 0)

    # Benchmark initial baseline price
    bm_prices = prices_map.get(bm_ticker, {})
    bm_base_price = None
    first_day_str = date_list[0].strftime("%Y-%m-%d") if date_list else None
    if first_day_str and first_day_str in bm_prices:
        bm_base_price = bm_prices[first_day_str]
    else:
        for d_check in sorted(bm_prices.keys()):
            bm_base_price = bm_prices[d_check]
            break
            
    last_known_bm_price = bm_base_price or 1.0

    # Simulate portfolio on each day
    for day_date in date_list:
        day_str = day_date.strftime("%Y-%m-%d")
        
        holdings_on_day = {}
        for tx in txs:
            tx_date = tx["order_date"]
            if isinstance(tx_date, dt):
                tx_d = tx_date.date()
            else:
                tx_d = tx_date
                
            if tx_d <= day_date:
                ticker = tx["ticker"].upper()
                qty = float(tx["quantity"])
                price = float(tx["buy_price"])
                tx_type = tx.get("transaction_type", "BUY").upper()
                
                if tx_type == "DIVIDEND":
                    continue
                    
                if ticker not in holdings_on_day:
                    holdings_on_day[ticker] = {"qty": 0.0, "avg_buy": 0.0}
                    
                h = holdings_on_day[ticker]
                if tx_type == "BUY":
                    if h["qty"] + qty > 0:
                        h["avg_buy"] = (h["qty"] * h["avg_buy"] + qty * price) / (h["qty"] + qty)
                    else:
                        h["avg_buy"] = 0.0
                    h["qty"] += qty
                elif tx_type == "SELL":
                    h["qty"] -= qty

                h["qty"] = round(h["qty"], 8)
                if h["qty"] <= 0:
                    h["qty"] = 0.0
                    h["avg_buy"] = 0.0
                        
        day_value = 0.0
        day_cost = 0.0
        
        for ticker, h in holdings_on_day.items():
            if h["qty"] > 0:
                ticker_prices = prices_map.get(ticker, {})
                price = ticker_prices.get(day_str)
                
                if price is None:
                    for lookback in range(1, 15):
                        prev_date = (day_date - timedelta(days=lookback)).strftime("%Y-%m-%d")
                        if prev_date in ticker_prices:
                            price = ticker_prices[prev_date]
                            break
                            
                if price is None:
                    price = h["avg_buy"]
                    
                day_value += h["qty"] * price
                day_cost += h["qty"] * h["avg_buy"]
                
        day_pl = day_value - day_cost
        day_pl_pct = (day_pl / day_cost * 100) if day_cost > 0 else 0.0
        
        # Benchmark value on this day
        bm_price = bm_prices.get(day_str)
        if bm_price is None:
            bm_price = last_known_bm_price
        else:
            last_known_bm_price = bm_price
            
        bm_return_pct = ((bm_price - bm_base_price) / bm_base_price * 100) if (bm_base_price and bm_base_price > 0) else 0.0
        
        history_data.append({
            "date": day_str,
            "portfolio_value": round(day_value, 2),
            "cost_basis": round(day_cost, 2),
            "profit_loss": round(day_pl, 2),
            "profit_loss_pct": round(day_pl_pct, 2),
            "benchmark_price": round(bm_price, 2),
            "benchmark_return_pct": round(bm_return_pct, 2)
        })
        
    # Calculate Quantitative Metrics
    metrics = {
        "start_date": first_day_str,
        "end_date": date_list[-1].strftime("%Y-%m-%d") if date_list else "",
        "period": period,
        "current_value": history_data[-1]["portfolio_value"] if history_data else 0.0,
        "current_cost": history_data[-1]["cost_basis"] if history_data else 0.0,
        "current_unrealized_pl": history_data[-1]["profit_loss"] if history_data else 0.0,
        "current_unrealized_pl_pct": history_data[-1]["profit_loss_pct"] if history_data else 0.0,
        "total_dividends": round(total_dividends_earned, 2),
        "total_fees": round(total_brokerage_fees + total_regulatory_fees + total_other_fees, 2),
        "brokerage_fees": round(total_brokerage_fees, 2),
        "regulatory_fees": round(total_regulatory_fees, 2),
        "other_fees": round(total_other_fees, 2)
    }

    # Calculate returns series for advanced metrics
    if len(history_data) >= 2:
        # Daily percentage changes & daily dollar changes (excluding capital buys/sells)
        daily_pct_changes = []
        daily_dollar_changes = []
        curr_peak = history_data[0]["portfolio_value"]
        max_dd = 0.0
        twr_factor = 1.0
        
        for i in range(len(history_data)):
            v = history_data[i]["portfolio_value"]
            if v > curr_peak:
                curr_peak = v
            dd = ((curr_peak - v) / curr_peak * 100) if curr_peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
                
            if i > 0:
                prev_v = history_data[i-1]["portfolio_value"]
                prev_c = history_data[i-1]["cost_basis"]
                curr_c = history_data[i]["cost_basis"]
                cap_injected = curr_c - prev_c
                
                # Pure market gain/loss excluding capital injected
                d_dollar = v - prev_v - cap_injected
                base_v = prev_v + max(0, cap_injected)
                if base_v > 0:
                    d_pct = (d_dollar / base_v) * 100.0
                elif prev_v > 0:
                    d_pct = (d_dollar / prev_v) * 100.0
                else:
                    d_pct = 0.0
                    
                daily_pct_changes.append(d_pct)
                daily_dollar_changes.append(d_dollar)
                twr_factor *= (1.0 + (d_pct / 100.0))

        # Time-Weighted Return (%) over the selected period
        port_ret_pct = (twr_factor - 1.0) * 100.0
        bm_ret_pct = history_data[-1]["benchmark_return_pct"]
        alpha = port_ret_pct - bm_ret_pct
                
        # Win Rate & Profit Factor
        pos_days = [d for d in daily_pct_changes if d > 0]
        neg_days = [d for d in daily_pct_changes if d < 0]
        win_rate = (len(pos_days) / len(daily_pct_changes) * 100) if daily_pct_changes else 0.0
        gross_gains = sum(pos_days)
        gross_losses = abs(sum(neg_days))
        profit_factor = (gross_gains / gross_losses) if gross_losses > 0 else (gross_gains if gross_gains > 0 else 1.0)
        
        # Volatility & Sharpe Ratio (rf = 4.5% annual = 0.045/252 daily)
        if len(daily_pct_changes) > 1:
            arr = np.array(daily_pct_changes) / 100.0
            mean_daily = np.mean(arr)
            std_daily = np.std(arr, ddof=1)
            ann_vol = std_daily * math.sqrt(252) * 100.0
            
            rf_daily = 0.045 / 252.0
            sharpe = ((mean_daily - rf_daily) / std_daily * math.sqrt(252)) if std_daily > 0 else 0.0
            
            # Sortino
            downside_arr = arr[arr < rf_daily]
            downside_std = np.std(downside_arr, ddof=1) if len(downside_arr) > 1 else std_daily
            sortino = ((mean_daily - rf_daily) / downside_std * math.sqrt(252)) if downside_std > 0 else sharpe
        else:
            ann_vol = 0.0
            sharpe = 0.0
            sortino = 0.0
            
        best_day_val = max(daily_dollar_changes) if daily_dollar_changes else 0.0
        worst_day_val = min(daily_dollar_changes) if daily_dollar_changes else 0.0
        best_day_pct = max(daily_pct_changes) if daily_pct_changes else 0.0
        worst_day_pct = min(daily_pct_changes) if daily_pct_changes else 0.0
        
        metrics.update({
            "portfolio_return_pct": round(port_ret_pct, 2),
            "benchmark_return_pct": round(bm_ret_pct, 2),
            "benchmark_ticker": bm_ticker,
            "alpha": round(alpha, 2),
            "beat_market": bool(alpha >= 0),
            "max_drawdown_pct": round(max_dd, 2),
            "annualized_volatility_pct": round(ann_vol, 2),
            "sharpe_ratio": round(sharpe, 2),
            "sortino_ratio": round(sortino, 2),
            "win_rate_pct": round(win_rate, 1),
            "profit_factor": round(profit_factor, 2),
            "best_day_dollar": round(best_day_val, 2),
            "best_day_pct": round(best_day_pct, 2),
            "worst_day_dollar": round(worst_day_val, 2),
            "worst_day_pct": round(worst_day_pct, 2)
        })
    else:
        metrics.update({
            "portfolio_return_pct": 0.0,
            "benchmark_return_pct": 0.0,
            "benchmark_ticker": bm_ticker,
            "alpha": 0.0,
            "beat_market": True,
            "max_drawdown_pct": 0.0,
            "annualized_volatility_pct": 0.0,
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "win_rate_pct": 0.0,
            "profit_factor": 1.0,
            "best_day_dollar": 0.0,
            "best_day_pct": 0.0,
            "worst_day_dollar": 0.0,
            "worst_day_pct": 0.0
        })

    # Asset Allocation Calculation
    latest_holdings = {}
    for tx in txs:
        ticker = tx["ticker"].upper()
        qty = float(tx["quantity"])
        price = float(tx["buy_price"])
        tx_type = tx.get("transaction_type", "BUY").upper()
        if tx_type == "DIVIDEND":
            continue
        if ticker not in latest_holdings:
            latest_holdings[ticker] = {"qty": 0.0, "avg_buy": 0.0}
        h = latest_holdings[ticker]
        if tx_type == "BUY":
            if h["qty"] + qty > 0:
                h["avg_buy"] = (h["qty"] * h["avg_buy"] + qty * price) / (h["qty"] + qty)
            h["qty"] += qty
        elif tx_type == "SELL":
            h["qty"] -= qty
            if h["qty"] <= 0:
                h["qty"] = 0.0
                h["avg_buy"] = 0.0
                
    allocation = []
    tot_val = metrics["current_value"]
    for ticker, h in latest_holdings.items():
        if h["qty"] > 0:
            cur_p = h["avg_buy"]
            t_prices = prices_map.get(ticker, {})
            if t_prices:
                cur_p = list(t_prices.values())[-1]
            c_val = h["qty"] * cur_p
            c_cost = h["qty"] * h["avg_buy"]
            unrealized = c_val - c_cost
            weight = (c_val / tot_val * 100) if tot_val > 0 else 0.0
            allocation.append({
                "ticker": ticker,
                "quantity": round(h["qty"], 4),
                "current_price": round(cur_p, 2),
                "market_value": round(c_val, 2),
                "cost_basis": round(c_cost, 2),
                "unrealized_pl": round(unrealized, 2),
                "weight_pct": round(weight, 2)
            })
    allocation.sort(key=lambda x: x["market_value"], reverse=True)

    # Monthly Performance Matrix
    monthly_data = {}
    for h in history_data:
        ym = h["date"][:7] # YYYY-MM
        if ym not in monthly_data:
            monthly_data[ym] = {
                "month": ym,
                "start_val": h["portfolio_value"],
                "end_val": h["portfolio_value"],
                "start_bm": h["benchmark_return_pct"],
                "end_bm": h["benchmark_return_pct"]
            }
        else:
            monthly_data[ym]["end_val"] = h["portfolio_value"]
            monthly_data[ym]["end_bm"] = h["benchmark_return_pct"]
            
    monthly_matrix = []
    for ym, m in sorted(monthly_data.items(), reverse=True):
        m_ret = ((m["end_val"] - m["start_val"]) / m["start_val"] * 100) if m["start_val"] > 0 else 0.0
        m_bm_ret = m["end_bm"] - m["start_bm"]
        m_alpha = m_ret - m_bm_ret
        monthly_matrix.append({
            "month": ym,
            "portfolio_return_pct": round(m_ret, 2),
            "benchmark_return_pct": round(m_bm_ret, 2),
            "alpha": round(m_alpha, 2),
            "beat_market": bool(m_alpha >= 0)
        })

    return {
        "history": history_data,
        "benchmark": [],
        "metrics": metrics,
        "allocation": allocation,
        "monthly_matrix": monthly_matrix
    }

def evaluate_tick_stoploss(price_cache):
    """Event-driven portfolio risk management using live 1-min data."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        
        # 1. Fetch current open positions from the shadow portfolio
        cur.execute(f"""
            SELECT ticker, 
                   SUM(CASE WHEN transaction_type = 'BUY' THEN quantity WHEN transaction_type = 'SELL' THEN -quantity ELSE 0 END) as net_qty
            FROM {settings.mimir_schema}.mimir_portfolio
            GROUP BY ticker
            HAVING SUM(CASE WHEN transaction_type = 'BUY' THEN quantity WHEN transaction_type = 'SELL' THEN -quantity ELSE 0 END) > 0.00001
        """)
        open_positions = cur.fetchall()
        
        alert_count = 0
        for pos in open_positions:
            ticker, qty = pos
            if ticker not in price_cache or len(price_cache[ticker]) == 0:
                continue
                
            current_price = price_cache[ticker][-1]['close']
            
            # Simple 2% trailing stop logic on the 50-min rolling high
            ticks = list(price_cache[ticker])
            window = ticks[-50:] if len(ticks) >= 50 else ticks
            recent_high = max([t['high'] for t in window])
            
            trailing_stop = recent_high * 0.98  # 2% drop from the local high
            
            if current_price <= trailing_stop:
                # To prevent spam, check if we already have a pending or recent trailing stop alert for this ticker
                cur.execute(f"""
                    SELECT id FROM {settings.mimir_schema}.mimir_trade_signals 
                    WHERE ticker = %s AND reason LIKE '%%Trailing Stop%%'
                      AND (status = 'PENDING' OR created_at >= NOW() - INTERVAL '12 hours')
                """, (ticker,))
                
                if not cur.fetchone():
                    print(f"[PORTFOLIO RISK] TRAILING STOP WARNING for {ticker}! Price {current_price} dropped below stop limit ({trailing_stop}). Generating alert.")
                    
                    reason = f"Trailing Stop Triggered: Price dropped below {trailing_stop:.2f} (2% trailing local high). Consider selling {qty} shares."
                    
                    cur.execute(f"""
                        INSERT INTO {settings.mimir_schema}.mimir_trade_signals
                        (ticker, signal_type, trigger_price, reason, status, created_at)
                        VALUES (%s, 'SELL', %s, %s, 'PENDING', NOW())
                    """, (ticker, current_price, reason))
                    alert_count += 1
                    
        conn.commit()
        if alert_count > 0:
            print(f"[PORTFOLIO RISK] Generated {alert_count} Trailing Stop warning alerts.")
    except Exception as e:
        conn.rollback()
        print(f"[PORTFOLIO RISK ERROR] {e}")
    finally:
        cur.close()
        conn.close()
