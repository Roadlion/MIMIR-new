import json
from datetime import datetime, timedelta
from typing import List, Dict, Any
from backend.app.database import get_db_connection_dict
from backend.app.scrapers.web_search import perform_tiered_search

def get_db_connection():
    return get_db_connection_dict()

def query_internal_news(ticker: str, days_back: int = 7) -> str:
    """Query recent news and sentiment impacts for a specific ticker."""
    query = """
        SELECT r.title, r.summary, i.sentiment_score, i.direction, r.published_ts
        FROM yggdrasil.mimir_sentiment_impacts i
        JOIN yggdrasil.mimir_raw_articles r ON i.article_id = r.id
        WHERE i.ticker = %s AND r.published_ts >= NOW() - INTERVAL '%s days'
        ORDER BY r.published_ts DESC
        LIMIT 10
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(query, (ticker, days_back))
        results = cur.fetchall()
        cur.close()
        conn.close()
        if not results:
            return f"No recent news found for {ticker} in the internal database."
        return json.dumps([dict(r) for r in results], default=str)
    except Exception as e:
        return f"Error querying news: {e}"

def query_asset_pricing(ticker: str, days_back: int = 7) -> str:
    """Query recent pricing (daily close) for an asset."""
    query = """
        SELECT timestamp, close, volume
        FROM yggdrasil.mimir_hourly_ohlcv
        WHERE ticker = %s AND timestamp >= NOW() - INTERVAL '%s days'
        ORDER BY timestamp DESC
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(query, (ticker, days_back))
        results = cur.fetchall()
        cur.close()
        conn.close()
        if not results:
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
        FROM yggdrasil.mimir_portfolio
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
        return json.dumps([dict(r) for r in results], default=str)
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
        FROM yggdrasil.mimir_trade_signals
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
        FROM yggdrasil.mimir_backtest_history
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
    query = """
        SELECT ticker, AVG(sentiment_score) as avg_sentiment, COUNT(*) as article_count
        FROM yggdrasil.mimir_sentiment_impacts i
        JOIN yggdrasil.mimir_raw_articles r ON i.article_id = r.id
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
            "description": "Retrieve the user's complete portfolio transaction history, trading logs, holdings, and cost basis. Returns ALL records by default unless limit is specified.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Optional ticker to filter transactions (e.g. AAPL)"},
                    "days_back": {"type": "integer", "description": "Optional number of days to look back"},
                    "limit": {"type": "integer", "description": "Optional limit on number of transactions to return. If omitted, retrieves ALL transactions."}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_trade_signals",
            "description": "Retrieve history of generated trade signals, automated/manual trade execution logs, and alert statuses (PENDING, APPROVED, REJECTED). Returns ALL records by default unless limit is specified.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Optional ticker to filter trade signals"},
                    "status": {"type": "string", "description": "Optional signal status filter (e.g. PENDING, APPROVED, REJECTED)"},
                    "days_back": {"type": "integer", "description": "Optional number of days to look back"},
                    "limit": {"type": "integer", "description": "Optional limit on number of signals returned. Defaults to returning all if omitted."}
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
                    "min_sentiment": {"type": "number", "description": "Minimum average sentiment score between -1.0 and 1.0 (default 0.5)"},
                    "limit": {"type": "integer", "description": "Maximum number of assets to return (default 10)"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_web_tool",
            "description": "Perform a live web search using DuckDuckGo/Tavily for breaking news, macro events, or general knowledge not in the internal database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query (e.g., 'Tesla Q3 earnings report summary')"}
                },
                "required": ["query"]
            }
        }
    }
]

# Dispatcher
def execute_oracle_tool(name: str, args: dict) -> str:
    if name == "query_internal_news":
        return query_internal_news(args.get("ticker"), args.get("days_back", 7))
    elif name == "query_asset_pricing":
        return query_asset_pricing(args.get("ticker"), args.get("days_back", 7))
    elif name == "query_portfolio":
        return query_portfolio(args.get("ticker"), args.get("days_back"), args.get("limit"))
    elif name == "query_trade_signals":
        return query_trade_signals(args.get("ticker"), args.get("status"), args.get("days_back"), args.get("limit"))
    elif name == "query_backtest_history":
        return query_backtest_history(args.get("limit"))
    elif name == "screen_assets":
        return screen_assets(args.get("min_sentiment", 0.5), args.get("limit", 10))
    elif name == "search_web_tool":
        return search_web_tool(args.get("query"))
    return f"Unknown tool: {name}"

