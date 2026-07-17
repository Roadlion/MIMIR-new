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

def query_portfolio() -> str:
    """Retrieve the user's current portfolio holdings and recent trades."""
    query = """
        SELECT ticker, transaction_type, quantity, buy_price, order_date
        FROM yggdrasil.mimir_portfolio
        ORDER BY order_date DESC
        LIMIT 20
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(query)
        results = cur.fetchall()
        cur.close()
        conn.close()
        if not results:
            return "Portfolio is currently empty."
        return json.dumps([dict(r) for r in results], default=str)
    except Exception as e:
        return f"Error querying portfolio: {e}"

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
            "description": "Retrieve the user's active portfolio holdings, transaction history, and cost basis.",
            "parameters": {
                "type": "object",
                "properties": {}
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
        return query_portfolio()
    elif name == "screen_assets":
        return screen_assets(args.get("min_sentiment", 0.5), args.get("limit", 10))
    elif name == "search_web_tool":
        return search_web_tool(args.get("query"))
    return f"Unknown tool: {name}"
