# backend/app/routers/sentiment.py
from fastapi import APIRouter, HTTPException, Query
from typing import Optional, List
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor
from ..database import get_db_connection_dict
from ..config import get_settings

router = APIRouter()
settings = get_settings()


@router.get("/summary")
async def get_sentiment_summary(days: int = Query(1, ge=1, le=30)):
    """Get overall sentiment summary for the last N days."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    # Get summary stats
    cur.execute(f"""
        SELECT 
            COUNT(DISTINCT a.id) AS total_articles,
            AVG(si.sentiment_score) AS avg_sentiment,
            COUNT(si.article_id) FILTER (WHERE si.direction = 'bullish') AS bullish_count,
            COUNT(si.article_id) FILTER (WHERE si.direction = 'bearish') AS bearish_count,
            COUNT(si.article_id) FILTER (WHERE si.direction = 'neutral') AS neutral_count
        FROM {settings.mimir_schema}.mimir_raw_articles a
        LEFT JOIN {settings.mimir_schema}.mimir_sentiment_impacts si ON a.id = si.article_id
        WHERE a.published_ts > NOW() - INTERVAL '%s days'
    """, (days,))
    
    result = cur.fetchone()
    
    # Get top mover (asset with most articles and extreme sentiment)
    cur.execute(f"""
        SELECT 
            si.asset_name,
            AVG(si.sentiment_score) AS avg_sentiment,
            COUNT(DISTINCT a.id) AS article_count
        FROM {settings.mimir_schema}.mimir_raw_articles a
        JOIN {settings.mimir_schema}.mimir_sentiment_impacts si ON a.id = si.article_id
        WHERE a.published_ts > NOW() - INTERVAL '%s days'
        GROUP BY si.asset_name
        HAVING COUNT(DISTINCT a.id) >= 3
        ORDER BY ABS(AVG(si.sentiment_score)) DESC, COUNT(DISTINCT a.id) DESC
        LIMIT 1
    """, (days,))
    
    top_mover = cur.fetchone()
    
    cur.close()
    conn.close()
    
    return {
        "total_articles": result.get("total_articles", 0) if result else 0,
        "avg_sentiment": float(result.get("avg_sentiment", 0)) if result else 0,
        "bullish_count": result.get("bullish_count", 0) if result else 0,
        "bearish_count": result.get("bearish_count", 0) if result else 0,
        "neutral_count": result.get("neutral_count", 0) if result else 0,
        "top_mover": top_mover.get("asset_name") if top_mover else None
    }


@router.get("/morning-report")
async def get_morning_report(limit: int = Query(10, ge=1, le=50)):
    """Get morning report: key headlines with sentiment for the last 24 hours."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    cur.execute(f"""
        SELECT 
            a.title,
            a.link,
            a.published_ts,
            si.asset_name,
            si.sentiment_score,
            si.direction,
            si.magnitude,
            si.reasoning
        FROM {settings.mimir_schema}.mimir_raw_articles a
        JOIN {settings.mimir_schema}.mimir_sentiment_impacts si ON a.id = si.article_id
        WHERE a.published_ts > NOW() - INTERVAL '24 hours'
          AND si.magnitude IN ('HIGH', 'MEDIUM')
        ORDER BY si.magnitude DESC, ABS(si.sentiment_score) DESC, a.published_ts DESC
        LIMIT %s
    """, (limit,))
    
    results = cur.fetchall()
    cur.close()
    conn.close()
    
    return {
        "items": [
            {
                "title": r.get("title"),
                "link": r.get("link"),
                "timestamp": r.get("published_ts"),
                "asset": r.get("asset_name"),
                "sentiment": float(r.get("sentiment_score", 0)),
                "direction": r.get("direction"),
                "magnitude": r.get("magnitude"),
                "reasoning": r.get("reasoning")
            }
            for r in results
        ]
    }


@router.get("/asset/{asset_name}")
async def get_asset_sentiment(
    asset_name: str,
    days: int = Query(7, ge=1, le=90)
):
    """Get sentiment history for a specific asset."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    cur.execute(f"""
        SELECT 
            DATE_TRUNC('hour', created_at) AS hour,
            AVG(sentiment_score) AS avg_sentiment,
            COUNT(*) AS article_count,
            COUNT(*) FILTER (WHERE direction = 'bullish') AS bullish_count,
            COUNT(*) FILTER (WHERE direction = 'bearish') AS bearish_count
        FROM {settings.mimir_schema}.mimir_sentiment_impacts
        WHERE asset_name ILIKE %s
          AND created_at > NOW() - INTERVAL '%s days'
        GROUP BY DATE_TRUNC('hour', created_at)
        ORDER BY hour ASC
    """, (asset_name, days))
    
    results = cur.fetchall()
    cur.close()
    conn.close()
    
    return {
        "asset_name": asset_name,
        "data": [
            {
                "hour": r.get("hour"),
                "sentiment": float(r.get("avg_sentiment", 0)) if r.get("avg_sentiment") else 0,
                "article_count": r.get("article_count", 0),
                "bullish": r.get("bullish_count", 0),
                "bearish": r.get("bearish_count", 0)
            }
            for r in results
        ]
    }