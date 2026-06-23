# backend/app/routers/articles.py
from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from datetime import datetime, timedelta
from ..database import get_db_connection_dict
from ..config import get_settings

router = APIRouter()
settings = get_settings()


@router.get("/articles")
async def get_articles(
    limit: int = Query(20, ge=1, le=200),
    source: Optional[str] = None,
    days: Optional[int] = Query(1, ge=1, le=30),
    asset: Optional[str] = None
):
    """Get articles with sentiment for the last N days."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    # Build query with sentiment aggregation
    sql = f"""
        SELECT 
            a.id,
            a.title,
            a.link,
            a.source_name,
            a.published_ts,
            a.summary,
            ARRAY_AGG(DISTINCT si.asset_name) FILTER (WHERE si.asset_name IS NOT NULL) AS assets,
            AVG(si.sentiment_score) AS sentiment_score
        FROM {settings.mimir_schema}.mimir_raw_articles a
        LEFT JOIN {settings.mimir_schema}.mimir_sentiment_impacts si ON a.id = si.article_id
        WHERE a.published_ts > NOW() - INTERVAL '%s days'
    """
    params = [days]
    
    if source:
        sql += " AND a.source_name = %s"
        params.append(source)
    
    if asset:
        sql += " AND si.asset_name ILIKE %s"
        params.append(f"%{asset}%")
    
    sql += """
        GROUP BY a.id, a.title, a.link, a.source_name, a.published_ts, a.summary
        ORDER BY a.published_ts DESC
        LIMIT %s
    """
    params.append(limit)
    
    cur.execute(sql, params)
    results = cur.fetchall()
    cur.close()
    conn.close()
    
    return {
        "count": len(results),
        "articles": [
            {
                "id": r.get("id"),
                "title": r.get("title"),
                "link": r.get("link"),
                "source_name": r.get("source_name"),
                "published_ts": r.get("published_ts"),
                "summary": r.get("summary"),
                "assets": r.get("assets") or [],
                "sentiment_score": float(r.get("sentiment_score")) if r.get("sentiment_score") else None
            }
            for r in results
        ]
    }


@router.get("/articles/{article_id}")
async def get_article(article_id: int):
    """Get a single article with its sentiment impacts."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    cur.execute(f"""
        SELECT 
            a.id, a.title, a.link, a.source_name, a.published_ts, a.summary,
            json_agg(
                json_build_object(
                    'asset_name', si.asset_name,
                    'ticker', si.ticker,
                    'sentiment_score', si.sentiment_score,
                    'confidence', si.confidence,
                    'direction', si.direction,
                    'magnitude', si.magnitude,
                    'reasoning', si.reasoning,
                    'policy_signal', si.policy_signal
                )
            ) FILTER (WHERE si.asset_name IS NOT NULL) AS impacts
        FROM {settings.mimir_schema}.mimir_raw_articles a
        LEFT JOIN {settings.mimir_schema}.mimir_sentiment_impacts si ON a.id = si.article_id
        WHERE a.id = %s
        GROUP BY a.id
    """, (article_id,))
    
    result = cur.fetchone()
    cur.close()
    conn.close()
    
    if not result:
        raise HTTPException(status_code=404, detail="Article not found")
    
    return result