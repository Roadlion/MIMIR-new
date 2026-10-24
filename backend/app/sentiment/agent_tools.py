import json
from datetime import datetime, timedelta
from typing import List, Dict, Any
from backend.app.database import get_db_connection_dict
from backend.app.config import get_settings
from backend.app.scrapers.web_search import perform_tiered_search

settings = get_settings()

def get_db_connection():
    return get_db_connection_dict()

def query_internal_news(ticker: str, days_back: int = 7) -> str:
    """Query recent news and sentiment impacts for a specific ticker."""
    ticker = (ticker or "").strip().upper()
    query = f"""
        SELECT r.title, r.summary, i.sentiment_score, i.direction, r.published_ts
        FROM {settings.mimir_schema}.mimir_sentiment_impacts i
        JOIN {settings.mimir_schema}.mimir_raw_articles r ON i.article_id = r.id
        WHERE i.ticker = %s AND r.published_ts >= NOW() - (%s || ' days')::INTERVAL
        ORDER BY r.published_ts DESC
        LIMIT 10
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(query, (ticker, int(days_back)))
        results = cur.fetchall()
        cur.close()
        conn.close()
        if not results:
            return f"No recent news found for {ticker} in the internal database."
        return json.dumps([dict(r) for r in results], default=str)
    except Exception as e:
        return f"Error querying news: {e}"

def query_asset_pricing(ticker: str, days_back: int = 7) -> str:
    """Query recent pricing (hourly/daily close) for an asset with live fallback."""
    ticker = (ticker or "").strip().upper()
    query = f"""
        SELECT timestamp, close, volume
        FROM {settings.mimir_schema}.mimir_hourly_ohlcv
        WHERE ticker = %s AND timestamp >= NOW() - (%s || ' days')::INTERVAL
        ORDER BY timestamp DESC
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(query, (ticker, int(days_back)))
        results = cur.fetchall()
        cur.close()
        conn.close()
        
        if not results:
            # Attempt live fetch & cache into SQL
            try:
                from backend.app.routers.prices import fetch_and_cache_ticker
                fetch_and_cache_ticker(ticker)
                # Re-query
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute(query, (ticker, int(days_back)))
                results = cur.fetchall()
                cur.close()
                conn.close()
            except Exception:
                pass

        if not results:
            # Fast info fallback
            try:
                from backend.app.routers.prices import _get_tls_session
                import yfinance as yf
                sess = _get_tls_session()
                yt = yf.Ticker(ticker, session=sess)
                info = yt.info or {}
                price = info.get("currentPrice") or info.get("previousClose")
                vol = info.get("volume", 0)
                if price:
                    return json.dumps([{
                        "timestamp": datetime.now().isoformat(),
                        "close": float(price),
                        "volume": vol,
                        "source": "live_quote"
                    }])
            except Exception:
                pass
            return f"No recent pricing data found for {ticker}."

        return json.dumps([dict(r) for r in results], default=str)
    except Exception as e:
        return f"Error querying pricing: {e}"

def query_portfolio(ticker: str = None, days_back: int = None, limit: int = None) -> str:
    """Retrieve the user's complete portfolio transaction history and holdings ledger. If limit is not specified, returns ALL records."""
    where_clauses = []
    params = []
    
    if ticker:
        where_clauses.append("ticker = %s")
        params.append(ticker.upper())
    if days_back and int(days_back) > 0:
        where_clauses.append("order_date >= NOW() - (%s || ' days')::INTERVAL")
        params.append(int(days_back))
        
    where_str = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    limit_clause = f"LIMIT {int(limit)}" if (limit is not None and int(limit) > 0) else ""
    
    query = f"""
        SELECT id, ticker, transaction_type, quantity, buy_price, order_date, brokerage_fee, regulatory_fee, other_fee
        FROM {settings.mimir_schema}.mimir_portfolio
        {where_str}
        ORDER BY order_date DESC
        {limit_clause}
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(query, params)
        results = cur.fetchall()
        cur.close()
        conn.close()
        if not results:
            return "Portfolio is currently empty or no matching transactions found."
        clean_results = []
        for r in results:
            d = dict(r)
            if d.get("quantity") is not None:
                d["quantity"] = round(float(d["quantity"]), 6)
            clean_results.append(d)
        return json.dumps(clean_results, default=str)
    except Exception as e:
        return f"Error querying portfolio: {e}"

def query_trade_signals(ticker: str = None, status: str = None, days_back: int = None, limit: int = None) -> str:
    """Retrieve history of generated trade signals, trade alert execution logs, and alert statuses."""
    where_clauses = []
    params = []
    
    if ticker:
        where_clauses.append("ticker = %s")
        params.append(ticker.upper())
    if status:
        where_clauses.append("status = %s")
        params.append(status.upper())
    if days_back and int(days_back) > 0:
        where_clauses.append("created_at >= NOW() - (%s || ' days')::INTERVAL")
        params.append(int(days_back))
        
    where_str = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    limit_clause = f"LIMIT {int(limit)}" if (limit is not None and int(limit) > 0) else ""
    
    query = f"""
        SELECT id, ticker, signal_type, trigger_price, rsi_value, sentiment_score, support_level, resistance_level, reason, status, created_at, acted_at
        FROM {settings.mimir_schema}.mimir_trade_signals
        {where_str}
        ORDER BY created_at DESC
        {limit_clause}
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(query, params)
        results = cur.fetchall()
        cur.close()
        conn.close()
        if not results:
            return "No matching trade signals or trade alert logs found."
        return json.dumps([dict(r) for r in results], default=str)
    except Exception as e:
        return f"Error querying trade signals: {e}"

def query_backtest_history(limit: int = None) -> str:
    """Retrieve historical quantitative backtest execution logs and strategy performance metrics."""
    limit_clause = f"LIMIT {int(limit)}" if (limit is not None and int(limit) > 0) else ""
    query = f"""
        SELECT id, formula, universe, style, start_date, end_date, holding_period, slippage_bps, portfolio_size, markets, sharpe, annualized_return, max_drawdown, turnover, fitness, win_rate, ic, created_at
        FROM {settings.mimir_schema}.mimir_backtest_history
        ORDER BY created_at DESC
        {limit_clause}
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(query)
        results = cur.fetchall()
        cur.close()
        conn.close()
        if not results:
            return "No historical backtests found."
        return json.dumps([dict(r) for r in results], default=str)
    except Exception as e:
        return f"Error querying backtest history: {e}"

def screen_assets(min_sentiment: float = 0.5, limit: int = 10) -> str:
    """Screen for assets with high recent sentiment scores."""
    query = f"""
        SELECT ticker, AVG(sentiment_score) as avg_sentiment, COUNT(*) as article_count
        FROM {settings.mimir_schema}.mimir_sentiment_impacts i
        JOIN {settings.mimir_schema}.mimir_raw_articles r ON i.article_id = r.id
        WHERE r.published_ts >= NOW() - INTERVAL '3 days'
        GROUP BY ticker
        HAVING AVG(sentiment_score) >= %s
        ORDER BY avg_sentiment DESC, article_count DESC
        LIMIT %s
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(query, (min_sentiment, limit))
        results = cur.fetchall()
        cur.close()
        conn.close()
        if not results:
            return f"No assets found with sentiment >= {min_sentiment} in the last 3 days."
        return json.dumps([dict(r) for r in results], default=str)
    except Exception as e:
        return f"Error screening assets: {e}"

def search_web_tool(query: str) -> str:
    """Perform a live web search for up-to-date information."""
    results = perform_tiered_search(query, num_results=3)
    if not results:
        return "No results found on the web."
    return json.dumps(results)

from backend.app.analytics import financial_skills

# Tool Definitions for DeepSeek Function Calling
ORACLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_internal_news",
            "description": "Query the internal MIMIR database for recent news articles, sentiment scores, and spillover impacts for a specific ticker.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "The stock ticker symbol (e.g. AAPL, TSLA)"},
                    "days_back": {"type": "integer", "description": "Number of days to look back (default 7)"}
                },
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_asset_pricing",
            "description": "Query recent hourly pricing and volume data for a specific ticker from the internal database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "The stock ticker symbol (e.g. AAPL, TSLA)"},
                    "days_back": {"type": "integer", "description": "Number of days to look back (default 7)"}
                },
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_portfolio",
            "description": "Retrieve the user's complete portfolio transaction history, trading logs, holdings, and cost basis.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Optional ticker to filter transactions"},
                    "days_back": {"type": "integer", "description": "Optional number of days to look back"},
                    "limit": {"type": "integer", "description": "Optional limit on number of transactions to return."}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_trade_signals",
            "description": "Retrieve history of generated trade signals, automated/manual trade execution logs, and alert statuses.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Optional ticker to filter trade signals"},
                    "status": {"type": "string", "description": "Optional signal status filter"},
                    "days_back": {"type": "integer", "description": "Optional number of days to look back"},
                    "limit": {"type": "integer", "description": "Optional limit on number of signals returned."}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_backtest_history",
            "description": "Retrieve historical quantitative backtest execution logs and strategy performance results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Optional limit on number of backtest logs returned."}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "screen_assets",
            "description": "Screen the market for assets with high recent average sentiment scores.",
            "parameters": {
                "type": "object",
                "properties": {
                    "min_sentiment": {"type": "number", "description": "Minimum average sentiment score"},
                    "limit": {"type": "integer", "description": "Maximum number of assets to return"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_web_tool",
            "description": "Perform a live web search for breaking news, macro events, or general knowledge.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_dcf_valuation",
            "description": "Run a deterministic 2-stage Discounted Cash Flow (DCF) valuation model to calculate intrinsic value per share and Margin of Safety for a ticker.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol (e.g. AAPL, NVDA)"}
                },
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_comps_analysis",
            "description": "Run a Comparable Company Analysis (Comps) peer matrix evaluating relative P/E, EV/EBITDA, P/S valuation vs industry peers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol (e.g. AAPL, NVDA)"}
                },
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_lbo_analysis",
            "description": "Run a Leveraged Buyout (LBO) financial model evaluating debt capacity, 5-year debt paydown, exit IRR, and equity multiplier.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol (e.g. AAPL, NVDA)"}
                },
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "review_earnings_report",
            "description": "Review quarterly earnings results, surprises, guidance changes, and Post-Earnings Announcement Drift (PEAD) momentum.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol (e.g. AAPL, NVDA)"}
                },
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "reconcile_portfolio_audit",
            "description": "Audit portfolio transactions, verify ledger integrity, fee calculations, cost basis consistency, and unhedged asset risks.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "audit_operational_costs",
            "description": "Audit MIMIR operational API token spend vs trading alpha yield to evaluate system self-funding efficiency.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "screen_capacity_constrained_assets",
            "description": "Screen small-cap/niche assets combining positive sentiment momentum with strong DCF Margin of Safety.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "generate_investment_pitch",
            "description": "Generate an institutional investment pitch deck memo combining DCF, Comps, LBO, Earnings, and Sentiment metrics.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Stock ticker symbol (e.g. AAPL, NVDA)"}
                },
                "required": ["ticker"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_docx_report",
            "description": "Create an institutional-grade Word (.docx) research document using the Create DOCX Skill Engine with custom styling, shaded tables, callout alert boxes, and executive headers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "The title of the document report"},
                    "markdown_content": {"type": "string", "description": "The markdown structured content to convert into styled Word document format"}
                },
                "required": ["title", "markdown_content"]
            }
        }
    }
]

from backend.app.utils.document_export import markdown_to_docx

# Dispatcher
def execute_oracle_tool(name: str, args: dict) -> str:
    raw_tk = args.get("ticker")
    tk = raw_tk.strip().upper() if raw_tk else "AAPL"

    if name == "query_internal_news":
        return query_internal_news(tk, args.get("days_back", 7))
    elif name == "query_asset_pricing":
        return query_asset_pricing(tk, args.get("days_back", 7))
    elif name == "query_portfolio":
        return query_portfolio(args.get("ticker"), args.get("days_back"), args.get("limit"))
    elif name == "query_trade_signals":
        return query_trade_signals(args.get("ticker"), args.get("status"), args.get("days_back"), args.get("limit"))
    elif name == "query_backtest_history":
        return query_backtest_history(args.get("limit"))
    elif name == "screen_assets":
        return screen_assets(args.get("min_sentiment", 0.5), args.get("limit", 10))
    elif name == "search_web_tool":
        return search_web_tool(args.get("query", ""))
    elif name == "run_dcf_valuation":
        return json.dumps(financial_skills.run_dcf_valuation(tk), default=str)
    elif name == "run_comps_analysis":
        return json.dumps(financial_skills.run_comps_analysis(tk), default=str)
    elif name == "run_lbo_analysis":
        return json.dumps(financial_skills.run_lbo_analysis(tk), default=str)
    elif name == "review_earnings_report":
        return json.dumps(financial_skills.review_earnings(tk), default=str)
    elif name == "reconcile_portfolio_audit":
        return json.dumps(financial_skills.reconcile_portfolio_audit(), default=str)
    elif name == "audit_operational_costs":
        return json.dumps(financial_skills.audit_operational_costs(), default=str)
    elif name == "screen_capacity_constrained_assets":
        return json.dumps(financial_skills.screen_capacity_constrained_assets(), default=str)
    elif name == "generate_investment_pitch":
        res = financial_skills.generate_pitch_pack(tk)
        return res.get("investment_memo_markdown", json.dumps(res))
    elif name == "create_docx_report":
        buf = markdown_to_docx(args.get("markdown_content", ""), title=args.get("title", "Research Report"))
        return f"[SUCCESS] Created styled DOCX document report ({len(buf.getvalue())} bytes). Available via Export button."
    return f"Unknown tool: {name}"



