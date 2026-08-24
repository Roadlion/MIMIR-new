# backend/app/routers/portfolio.py
from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel
from typing import List, Optional, Dict
from datetime import datetime, timezone
import requests
import json
import re
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor

from ..database import get_db_connection_dict, get_db_connection
from ..config import get_settings
from ..auth import get_optional_current_user, get_current_user
from ..sentiment.llm_client import send_chat_completion

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

# Helper to fetch current prices using yfinance
def fetch_current_prices(tickers: List[str]) -> Dict[str, float]:
    if not tickers:
        return {}
    prices = {}
    
    from curl_cffi.requests import Session
    session = Session(impersonate="chrome")
    session.verify = False
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    })

    def fetch_single(t_symbol):
        try:
            clean_symbol = t_symbol.strip().lstrip('$').upper()
            t = yf.Ticker(clean_symbol, session=session)
            # Try history first, it is most reliable under anti-bot protection
            hist = t.history(period="1d")
            if not hist.empty:
                return t_symbol, float(hist["Close"].iloc[-1])
            # Try fast_info fallback
            val = t.fast_info.get("lastPrice")
            if val is not None and not isinstance(val, str):
                return t_symbol, float(val)
        except Exception:
            pass
        return t_symbol, None

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = executor.map(fetch_single, tickers)
        for t_symbol, price in results:
            if price is not None:
                prices[t_symbol] = price
            else:
                prices[t_symbol] = 0.0 # Default fallback
                
    return prices

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
            if qty_sum <= 0:
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
            
        cost_sum = qty_sum * avg_buy
        curr_val = qty_sum * curr_price
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
            current_qty = 0.0
            for etx in existing_txs:
                etype = etx["transaction_type"].upper()
                eqty = float(etx["quantity"])
                if etype == "BUY":
                    current_qty += eqty
                elif etype == "SELL":
                    current_qty -= eqty
            
            if tx.quantity > current_qty:
                raise HTTPException(status_code=400, detail=f"Cannot sell {tx.quantity} shares of {tx.ticker}. You only own {current_qty} shares.")
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
        """, (user_id, tx.ticker.upper().strip(), localized_date, tx.buy_price, tx.quantity, tx.transaction_type.upper(), tx.brokerage_fee or 0.0, tx.regulatory_fee or 0.0, tx.other_fee or 0.0))
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
            
            qty_running = 0.0
            for item in all_proposed_sorted:
                itype = item["transaction_type"].upper()
                iqty = float(item["quantity"])
                if itype == "BUY":
                    qty_running += iqty
                elif itype == "SELL":
                    qty_running -= iqty
                if qty_running < 0:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Proposed changes would result in a negative holding quantity ({qty_running}) for {old_ticker} at {item['order_date']}."
                    )
        else:
            # Validate old ticker's inventory (excluding edited transaction)
            old_sorted = sorted(old_ticker_txs, key=lambda x: x["order_date"])
            qty_running_old = 0.0
            for item in old_sorted:
                itype = item["transaction_type"].upper()
                iqty = float(item["quantity"])
                if itype == "BUY":
                    qty_running_old += iqty
                elif itype == "SELL":
                    qty_running_old -= iqty
                if qty_running_old < 0:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Removing this transaction would result in negative holding quantity ({qty_running_old}) for {old_ticker} at {item['order_date']}."
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
            qty_running_new = 0.0
            for item in new_sorted:
                itype = item["transaction_type"].upper()
                iqty = float(item["quantity"])
                if itype == "BUY":
                    qty_running_new += iqty
                elif itype == "SELL":
                    qty_running_new -= iqty
                if qty_running_new < 0:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Proposed changes would result in a negative holding quantity ({qty_running_new}) for {new_ticker} at {item['order_date']}."
                    )

        # 3. Perform the update
        cur.execute(f"""
            UPDATE {settings.mimir_schema}.mimir_portfolio
            SET ticker = %s, order_date = %s, buy_price = %s, quantity = %s, transaction_type = %s,
                brokerage_fee = %s, regulatory_fee = %s, other_fee = %s
            WHERE id = %s AND user_id = %s
            RETURNING id, ticker, order_date, buy_price, quantity, transaction_type, created_at, brokerage_fee, regulatory_fee, other_fee
        """, (new_ticker, localized_date, tx.buy_price, tx.quantity, tx.transaction_type.upper(),
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

def fetch_online_stock_data(tickers: List[str]) -> Dict[str, List[str]]:
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
    
    news_by_ticker = {}
    for ticker in tickers:
        try:
            t = yf.Ticker(ticker, session=session)
            news = t.news
            headlines = [n["title"] for n in news[:4]] if news else []
            news_by_ticker[ticker] = headlines
        except Exception:
            news_by_ticker[ticker] = []
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
    
    from curl_cffi.requests import Session
    session = Session(impersonate="chrome")
    session.verify = False
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    })

    def fetch_single(t_symbol):
        try:
            clean_symbol = t_symbol.strip().lstrip('$').upper()
            t = yf.Ticker(clean_symbol, session=session)
            
            # Fetch 200 days of history for technical analysis
            hist = t.history(period="200d")
            techs = {
                "rsi_14": 50.0,
                "dma_50": 0.0,
                "dma_200": 0.0,
                "volume_ratio": 1.0,
                "price_trend_status": "Neutral"
            }
            
            current_price = 0.0
            change_5d_pct = 0.0
            
            if not hist.empty:
                current_price = float(hist["Close"].iloc[-1])
                price_5d_ago = float(hist["Close"].iloc[-5]) if len(hist) >= 5 else float(hist["Close"].iloc[0])
                change_5d_pct = ((current_price - price_5d_ago) / price_5d_ago * 100) if price_5d_ago > 0 else 0.0
                
                # Compute Moving Averages
                closes = hist["Close"]
                volumes = hist["Volume"]
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
                    
                # Compute Volume Ratio
                vol_ratio = 1.0
                if len(volumes) >= 5:
                    avg_vol = float(volumes.tail(30).mean())
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
                    
                techs = {
                    "rsi_14": round(rsi14, 2),
                    "dma_50": round(dma50, 2),
                    "dma_200": round(dma200, 2),
                    "volume_ratio": round(vol_ratio, 2),
                    "price_trend_status": trend_status
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
                        "price_trend_status": "Neutral"
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
            "advice": "Add some transactions to your portfolio first, and MIMIR will analyze them against market sentiment!"
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
        # Sort chronologically to compute weighted average cost basis and realized P&L
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
            if qty_sum <= 0:
                qty_sum = 0.0
                avg_buy = 0.0
        if qty_sum > 0:
            portfolio_list.append({
                "ticker": ticker,
                "quantity": qty_sum,
                "avg_price": avg_buy,
                "realized_pl": realized_pl
            })
            
    tickers_list = [p["ticker"] for p in portfolio_list]
    tickers_tuple = tuple(tickers_list)
    
    # 2. Get recent sentiment impacts for the user's stocks
    sentiment_data = []
    if tickers_tuple:
        # Avoid tuple syntax error for single item
        if len(tickers_tuple) == 1:
            query = f"""
                SELECT si.ticker, AVG(si.sentiment_score) as avg_score, COUNT(DISTINCT a.id) as article_count,
                       json_agg(json_build_object('title', a.title, 'reasoning', si.reasoning, 'score', si.sentiment_score)) as articles
                FROM {settings.mimir_schema}.mimir_sentiment_impacts si
                JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
                WHERE si.ticker = %s AND a.published_ts > NOW() - INTERVAL '14 days'
                GROUP BY si.ticker
            """
            cur.execute(query, (tickers_tuple[0],))
        else:
            query = f"""
                SELECT si.ticker, AVG(si.sentiment_score) as avg_score, COUNT(DISTINCT a.id) as article_count,
                       json_agg(json_build_object('title', a.title, 'reasoning', si.reasoning, 'score', si.sentiment_score)) as articles
                FROM {settings.mimir_schema}.mimir_sentiment_impacts si
                JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
                WHERE si.ticker IN %s AND a.published_ts > NOW() - INTERVAL '14 days'
                GROUP BY si.ticker
            """
            cur.execute(query, (tickers_tuple,))
        sentiment_data = cur.fetchall()
        
    # 3. Get top positive sentiment equities in the last 7 days as candidates for stock picks
    query_picks = f"""
        SELECT si.ticker, si.asset_name, AVG(si.sentiment_score) as score, COUNT(DISTINCT a.id) as count
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
    
    # 4. Look online for real-time news headlines, macro trend data, and recent price trends
    online_news = fetch_online_stock_data(tickers_list)
    macro_trends = fetch_macro_indicators()
    price_trends = fetch_portfolio_price_trends(tickers_list)
    
    # Enrich portfolio_list with profit, price movement, technical indicators, and fundamental metrics
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
                "price_trend_status": "Neutral"
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
            "unrealized_profit_loss": unrealized_pl,
            "unrealized_profit_loss_pct": unrealized_pl_pct,
            "realized_profit_loss": realized_pl,
            "total_profit_loss": unrealized_pl + realized_pl,
            "recent_price_movement": {
                "current_price": current_price,
                "change_5d_pct": trend["change_5d_pct"]
            },
            "technical_analysis": trend.get("technical_analysis", {}),
            "fundamental_analysis": trend.get("fundamental_analysis", {})
        })
        
    # 5. Formulate Prompt for DeepSeek
    prompt_context = {
        "portfolio": enriched_portfolio,
        "portfolio_sentiment": [
            {
                "ticker": s["ticker"],
                "avg_sentiment_score": float(s["avg_score"]),
                "article_count": s["article_count"],
                "recent_headlines": [art["title"] for art in s["articles"][:3]]
            }
            for s in sentiment_data
        ],
        "online_recent_news": online_news,
        "online_macro_trends": macro_trends,
        "top_sentiment_candidates": [
            {
                "ticker": p["ticker"],
                "name": p["asset_name"],
                "sentiment_score": float(p["score"]),
                "mentions": p["count"]
            }
            for p in picks
        ]
    }
    
    system_prompt = (
        "You are MIMIR's Senior Investment Strategist, an expert quantitative portfolio manager and macro economist. "
        "Your task is to analyze the user's portfolio data, technical analysis metrics, fundamental valuation metrics, "
        "market sentiment, online news, and macroeconomic indicators, "
        "and generate a highly professional, visually beautiful, and deeply insightful strategic report in HTML format. "
        "Write in a sharp, professional financial-analyst tone. Avoid fluff. "
        "STRICT GROUNDING RULE: You must rely SOLELY on the explicit technical indicators and fundamental metrics provided in the prompt context JSON (e.g. `technical_analysis` and `fundamental_analysis`). Do NOT hallucinate, approximate, or invent any indicators or ratios. If a metric is missing or null in the context, do not supply a placeholder or look it up; simply omit it from your reasoning or write 'N/A'."
    )
    user_prompt = f"""
You are provided with the following real-time and historical context:
---
CONTEXT DATA:
{json.dumps(prompt_context, indent=2)}
---

Generate a comprehensive strategic briefing for the user's portfolio. The output MUST be raw HTML fragments (do not wrap in ```html or other markdown blocks; do NOT include <style>, <script>, <html>, <head>, or <body> tags; just start with the HTML elements directly).
Use Tailwind CSS classes to style the output so it looks premium, sleek, and matches a high-end terminal dashboard (dark theme, using the application's palette of dark slate, emerald, cyan, amber, and gold/yellow).

STRICT HTML & STYLING DIRECTIVES:
- Do NOT include <style> blocks, CSS definitions, or custom CSS rules under any circumstances.
- Do NOT include <script> tags, external links, or stylesheet links.

STRICT DATA INTEGRITY DIRECTIVES:
- Do NOT search online, use pre-trained general knowledge, or fabricate numbers.
- You MUST only use the exact values present in the CONTEXT DATA JSON under `technical_analysis` (RSI, DMAs, Volume Ratios, Trend Status) and `fundamental_analysis` (P/E, forward P/E, debt, profit margin) for each ticker.
- If an indicator or metric is missing, write 'N/A' or omit it from your reasoning.

Ensure the HTML includes the following 4 sections, designed beautifully:

1. <div class="mb-6">
     <h3 class="text-xl font-bold text-[#00A6B2] mb-3">📈 Portfolio Performance & Allocation Advice</h3>
     <p class="text-[#8BA4A8] text-sm mb-3">
       Provide a qualitative and quantitative assessment of the current portfolio's concentration, diversification, and general risk profile. 
       Analyze each position by overlaying **Technical Analysis** (price relation to 50/200 DMAs, RSI momentum, volume ratios), **Fundamental Analysis** (P/E ratios, profit margins, debt ratios), and **Sentiment**.
     </p>
     <div class="overflow-x-auto mb-4">
       <table class="w-full text-left text-xs text-[#D6E5E3] border-collapse">
         <thead>
           <tr class="border-b border-[#1A2A30] text-[#8BA4A8]">
             <th class="py-2 px-3">Ticker</th>
             <th class="py-2 px-3">Value</th>
             <th class="py-2 px-3">Unrealized P&L</th>
             <th class="py-2 px-3">Action</th>
             <th class="py-2 px-3">Rationale (TA + FA + Sentiment)</th>
           </tr>
         </thead>
         <tbody>
           <!-- Generate rows for each ticker in the portfolio here -->
         </tbody>
       </table>
     </div>
   </div>

2. <div class="mb-6">
     <h3 class="text-xl font-bold text-[#ffd700] mb-3">🔥 Top Sentiment & Fundamental Picks</h3>
     <p class="text-[#8BA4A8] text-sm mb-3">Recommend 2-3 stocks from 'top_sentiment_candidates' or select other high-performing names. Combine sentiment momentum with fundamental strength and technical breakout characteristics to justify these recommendations.</p>
   </div>

3. <div class="mb-6">
     <h3 class="text-xl font-bold text-[#00E676] mb-3">🌍 Macroeconomic Outlook & Market Regime</h3>
     <p class="text-[#8BA4A8] text-sm mb-3">Synthesize the global indicators from 'online_macro_trends' (S&P 500, Nasdaq, VIX, US Dollar, Gold, US 10Y Yield). Identify the prevailing market regime (e.g., Risk-On, Risk-Off) and discuss how it directly impacts the user's specific holdings.</p>
   </div>

4. <div class="mb-4">
     <h3 class="text-xl font-bold text-[#00E5F2] mb-3">💰 Alternative MIMIR Profit Strategies</h3>
     <ul class="list-disc list-inside text-sm text-[#D6E5E3] space-y-2">
       <li><strong>Swing Trading Sentinel:</strong> Describe how to set up alerts and swing trade stocks when sentiment swings heavily into bullish (>0.40) or bearish (&lt;-0.40) zones.</li>
       <li><strong>Guerilla Arbitrage:</strong> Explain how to leverage the 'Guerilla Quant' tab to identify co-integrated statistical arbitrage pairs (e.g., tracking spread deviation from Z-score thresholds).</li>
       <li><strong>Volume Anomaly Trigger:</strong> Outline how tracking abnormal volume spikes (relative volume > 2.0) alongside positive sentiment changes serves as a confirmation indicator for momentum breakouts.</li>
     </ul>
   </div>

For the "Action" column in the table, please use one of these HTML badges exactly:
- <span class="bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 px-2 py-0.5 rounded text-xs font-bold font-mono">BUY</span>
- <span class="bg-amber-500/10 text-amber-400 border border-amber-500/20 px-2 py-0.5 rounded text-xs font-bold font-mono">HOLD</span>
- <span class="bg-orange-500/10 text-orange-400 border border-orange-500/20 px-2 py-0.5 rounded text-xs font-bold font-mono">TRIM</span>
- <span class="bg-red-500/10 text-red-400 border border-red-500/20 px-2 py-0.5 rounded text-xs font-bold font-mono">SELL</span>

For the "Rationale" column in the table, you MUST synthesize Technical Analysis (e.g. RSI, 50 DMA, Golden Cross), Fundamental Analysis (e.g. Forward P/E, debt profile), and Sentiment trends into a cohesive, institutional-grade single-sentence argument. Use ONLY the technical and fundamental metrics provided in the CONTEXT DATA.

Format values nicely (e.g. prefixing dollar amounts with $, formatting percentages to 2 decimals). Do not include any greeting or conversational filler. Start directly with the first section's HTML wrapper.
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
            timeout=60
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
        content = content.strip()
        
        return {"advice": content}
    except Exception as e:
        return {
            "advice": f"<p class='text-[#FF5252]'>Error generating AI advice: {str(e)}. Please try again later.</p>"
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
                   SUM(CASE WHEN transaction_type = 'BUY' THEN quantity ELSE -quantity END) as net_qty
            FROM {settings.mimir_schema}.mimir_portfolio
            GROUP BY ticker
            HAVING SUM(CASE WHEN transaction_type = 'BUY' THEN quantity ELSE -quantity END) > 0
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
