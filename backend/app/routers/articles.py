# backend/app/routers/articles.py
from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from app.database import get_db_connection_dict
from app.config import get_settings

router = APIRouter()
settings = get_settings()

@router.get("/articles")
def get_articles(
    limit: int = Query(100, ge=1, le=1000),
    source: Optional[str] = None,
    days: Optional[int] = None
):
    """Get recent articles from the DB."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    sql = f"""
    SELECT id, source_name, title, link, published_ts, summary, scraped_at
    FROM {settings.mimir_schema}.mimir_raw_articles
    WHERE 1=1
    """
    params = []
    
    if source:
        sql += " AND source_name = %s"
        params.append(source)
    
    if days:
        sql += " AND scraped_at >= NOW() - INTERVAL '%s days'"
        params.append(days)
    
    sql += " ORDER BY published_ts DESC NULLS LAST LIMIT %s"
    params.append(limit)
    
    cur.execute(sql, params)
    results = cur.fetchall()
    cur.close()
    conn.close()
    
    return {"count": len(results), "articles": results}

@router.get("/articles/{article_id}")
def get_article(article_id: int):
    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    cur.execute(f"""
        SELECT id, source_name, feed_url, title, link, 
               published_raw, published_ts, summary, url_hash, scraped_at
        FROM {settings.mimir_schema}.mimir_raw_articles
        WHERE id = %s
    """, (article_id,))
    
    result = cur.fetchone()
    cur.close()
    conn.close()
    
    if not result:
        raise HTTPException(status_code=404, detail="Article not found")
    
    return result

@router.get("/sources")
def get_sources():
    """Get list of unique sources."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    cur.execute(f"""
        SELECT DISTINCT source_name, COUNT(*) as article_count
        FROM {settings.mimir_schema}.mimir_raw_articles
        GROUP BY source_name
        ORDER BY article_count DESC
    """)
    
    results = cur.fetchall()
    cur.close()
    conn.close()
    
    return {"sources": results}