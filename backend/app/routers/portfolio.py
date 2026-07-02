# backend/app/routers/portfolio.py
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import List, Optional, Dict
from datetime import datetime, timezone
import requests
import json
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor

from ..database import get_db_connection_dict, get_db_connection
from ..config import get_settings
from ..sentiment.llm_client import send_chat_completion

router = APIRouter()
settings = get_settings()

class TransactionCreate(BaseModel):
    ticker: str
    order_date: datetime
    buy_price: float
    quantity: float
    transaction_type: str = "BUY"

class TransactionResponse(BaseModel):
    id: int
    ticker: str
    order_date: datetime
    buy_price: float
    quantity: float
    transaction_type: str
    created_at: datetime

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
    transactions: List[Dict]

class PortfolioSummary(BaseModel):
    holdings: Dict[str, HoldingDetail]
    total_cost: float
    total_value: float
    total_profit_loss: float
    total_profit_loss_pct: float
    total_realized_pl: float
    grand_total_pl: float

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

@router.get("/portfolio", response_model=PortfolioSummary)
def get_portfolio():
    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    # Fetch all transactions
    cur.execute(f"""
        SELECT id, ticker, order_date, buy_price, quantity, created_at, transaction_type
        FROM {settings.mimir_schema}.mimir_portfolio
        ORDER BY order_date DESC
    """)
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
            "grand_total_pl": 0.0
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
    current_prices = fetch_current_prices(tickers)
    
    holdings = {}
    total_cost = 0.0
    total_value = 0.0
    total_realized_pl = 0.0
    
    for ticker, txs in raw_holdings.items():
        # Sort chronologically to compute weighted average cost basis and realized P&L
        txs_sorted = sorted(txs, key=lambda x: x["order_date"])
        
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
            "transactions": [
                {
                    "id": tx["id"],
                    "order_date": tx["order_date"],
                    "buy_price": float(tx["buy_price"]),
                    "quantity": float(tx["quantity"]),
                    "transaction_type": tx.get("transaction_type", "BUY"),
                    "created_at": tx["created_at"]
                }
                for tx in txs
            ]
        }
        
    total_pl = total_value - total_cost
    total_pl_pct = (total_pl / total_cost * 100) if total_cost > 0 else 0.0
    grand_total = total_pl + total_realized_pl
    
    return {
        "holdings": holdings,
        "total_cost": total_cost,
        "total_value": total_value,
        "total_profit_loss": total_pl,
        "total_profit_loss_pct": total_pl_pct,
        "total_realized_pl": total_realized_pl,
        "grand_total_pl": grand_total
    }

@router.post("/portfolio", response_model=TransactionResponse)
def add_transaction(tx: TransactionCreate):
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
                WHERE ticker = %s
            """, (tx.ticker.upper().strip(),))
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
            INSERT INTO {settings.mimir_schema}.mimir_portfolio (ticker, order_date, buy_price, quantity, transaction_type)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, ticker, order_date, buy_price, quantity, transaction_type, created_at
        """, (tx.ticker.upper().strip(), localized_date, tx.buy_price, tx.quantity, tx.transaction_type.upper()))
        new_tx = cur.fetchone()
        conn.commit()
        return new_tx
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cur.close()
        conn.close()

@router.delete("/portfolio/{tx_id}")
def delete_transaction(tx_id: int):
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        cur.execute(f"""
            DELETE FROM {settings.mimir_schema}.mimir_portfolio
            WHERE id = %s
        """, (tx_id,))
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

@router.get("/portfolio/advice")
def get_portfolio_advice():
    # 1. Fetch current portfolio
    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    cur.execute(f"""
        SELECT ticker, buy_price, quantity
        FROM {settings.mimir_schema}.mimir_portfolio
    """)
    txs = cur.fetchall()
    
    if not txs:
        cur.close()
        conn.close()
        return {
            "advice": "Add some transactions to your portfolio first, and MIMIR will analyze them against market sentiment!"
        }
        
    # Group and aggregate
    portfolio_summary = {}
    for tx in txs:
        t = tx["ticker"].upper()
        if t not in portfolio_summary:
            portfolio_summary[t] = {"qty": 0.0, "cost": 0.0}
        portfolio_summary[t]["qty"] += float(tx["quantity"])
        portfolio_summary[t]["cost"] += float(tx["buy_price"]) * float(tx["quantity"])
        
    portfolio_list = []
    for t, info in portfolio_summary.items():
        if info["qty"] > 0:
            portfolio_list.append({
                "ticker": t,
                "quantity": info["qty"],
                "avg_price": info["cost"] / info["qty"]
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
    
    # 4. Look online for real-time news headlines and macro trend data
    online_news = fetch_online_stock_data(tickers_list)
    macro_trends = fetch_macro_indicators()
    
    # 5. Formulate Prompt for DeepSeek
    prompt_context = {
        "portfolio": portfolio_list,
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
    
    system_prompt = "You are MIMIR's Senior Investment Strategist. Output clear, concise investment strategy insights and stock suggestions."
    user_prompt = f"""
Analyze the user's shadow portfolio, recent local sentiment impacts, real-time online headlines, and key global macro trends to provide actionable recommendations.
Context:
{json.dumps(prompt_context, indent=2)}

Please structure your response in HTML format (using tailwind CSS classes where appropriate for typography/tables/lists, but keeping it clean). Do not include markdown wrappers (like ```html).
Sections required:
1. <div class="mb-6"><h3 class="text-xl font-bold text-[#00A6B2] mb-3">📈 Portfolio Performance & Allocation Advice</h3><p class="text-[#8BA4A8] text-sm mb-2">Provide a qualitative assessment of the allocation and diversification. Then recommend an action (Buy, Hold, Trim, Sell) for each ticker in the portfolio with a concise 1-2 sentence rationale linked to recent sentiment/news and online news headlines.</p></div>
2. <div class="mb-6"><h3 class="text-xl font-bold text-[#ffd700] mb-3">🔥 Top Sentiment Stock Picks</h3><p class="text-[#8BA4A8] text-sm mb-3">Recommend 2-3 stocks from the 'top_sentiment_candidates' or high quality sentiment names. For each, give the ticker, name, and a 1-sentence bull case explaining why the sentiment is so strong.</p></div>
3. <div class="mb-6"><h3 class="text-xl font-bold text-[#00E676] mb-3">🌍 Macroeconomic Outlook</h3><p class="text-[#8BA4A8] text-sm mb-2">Synthesize the 'online_macro_trends' (S&P 500, Nasdaq, VIX, US Dollar, Gold, US 10Y Yield) to evaluate overall market regime (e.g. Risk-On vs Risk-Off, hawkish/dovish indicators) and how it affects the user's portfolio.</p></div>
4. <div class="mb-4"><h3 class="text-xl font-bold text-[#00E5F2] mb-3">💰 Alternative MIMIR Profit Strategies</h3><ul class="list-disc list-inside text-sm text-[#D6E5E3] space-y-2"><li><strong>Swing Trading Sentinel:</strong> Describe how to swing trade stocks when sentiment swings heavily into bullish (>0.40) or bearish (<-0.40) ranges.</li><li><strong>Guerilla Arbitrage:</strong> Explain how to use MIMIR's Guerilla Quant tab to find statistical arbitrage pairs and monitor their spread.</li><li><strong>Volume Anomaly Trigger:</strong> Outline how tracking volume spikes alongside news sentiment is a powerful signal for breakout trading.</li></ul></div>
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
        
        # Clean markdown code block wraps if LLM returns them
        if content.startswith("```html"):
            content = content.replace("```html", "", 1)
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
        
        return {"advice": content}
    except Exception as e:
        return {
            "advice": f"<p class='text-[#FF5252]'>Error generating AI advice: {str(e)}. Please try again later.</p>"
        }
