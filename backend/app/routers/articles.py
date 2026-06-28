# backend/app/routers/articles.py
from fastapi import APIRouter, HTTPException, Query
from typing import Optional, List, Dict, Any
from ..database import get_db_connection_dict
from ..config import get_settings

router = APIRouter()
settings = get_settings()


@router.get("/articles")
async def get_articles(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    source: Optional[str] = None,
    asset: Optional[str] = None,
    sentiment: Optional[str] = None,
    days: Optional[int] = Query(None, ge=1, le=90),
    region: Optional[str] = None,
    country: Optional[str] = None,
    sort: Optional[str] = None
):
    """Get articles with sentiment impacts, with filters and offset pagination."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    where_clauses = []
    params = []
    
    if days is not None:
        where_clauses.append("COALESCE(a.published_ts, a.scraped_at) > NOW() - (%s * INTERVAL '1 day')")
        params.append(days)
        
    if search:
        where_clauses.append("(a.title ILIKE %s OR a.summary ILIKE %s)")
        params.extend([f"%{search}%", f"%{search}%"])
        
    if source:
        where_clauses.append("a.source_name = %s")
        params.append(source)
        
    if asset or sentiment:
        exists_clause = f"""
            EXISTS (
                SELECT 1 FROM {settings.mimir_schema}.mimir_sentiment_impacts si_filter 
                WHERE si_filter.article_id = a.id
        """
        if asset:
            exists_clause += " AND (si_filter.asset_name ILIKE %s OR si_filter.ticker ILIKE %s)"
            params.extend([f"%{asset}%", f"%{asset}%"])
        if sentiment:
            exists_clause += " AND si_filter.direction = %s"
            params.append(sentiment)
        exists_clause += ")"
        where_clauses.append(exists_clause)

    if region:
        where_clauses.append(f"""
            EXISTS (
                SELECT 1 FROM {settings.mimir_schema}.mimir_sentiment_impacts si_reg 
                WHERE si_reg.article_id = a.id AND si_reg.region = %s
            )
        """)
        params.append(region)

    if country:
        where_clauses.append(f"""
            EXISTS (
                SELECT 1 FROM {settings.mimir_schema}.mimir_sentiment_impacts si_cnt 
                WHERE si_cnt.article_id = a.id AND si_cnt.country = %s
            )
        """)
        params.append(country)
        
    where_str = ""
    if where_clauses:
        where_str = "WHERE " + " AND ".join(where_clauses)
        
    # 1. Fetch total count for pagination
    count_sql = f"""
        SELECT COUNT(*) 
        FROM {settings.mimir_schema}.mimir_raw_articles a
        {where_str}
    """
    cur.execute(count_sql, params)
    total_count = cur.fetchone()['count']
    
    # 2. Fetch paginated records
    offset = (page - 1) * limit
    data_sql = f"""
        SELECT 
            a.id,
            a.title,
            a.link,
            a.source_name,
            COALESCE(a.published_ts, a.scraped_at) AS published_ts,
            a.summary,
            COALESCE(
                json_agg(
                    json_build_object(
                        'asset_name', si.asset_name,
                        'ticker', si.ticker,
                        'sentiment_score', si.sentiment_score,
                        'direction', si.direction,
                        'reasoning', si.reasoning
                    )
                ) FILTER (WHERE si.asset_name IS NOT NULL),
                '[]'::json
            ) AS impacts,
            AVG(si.sentiment_score) AS sentiment_score
        FROM {settings.mimir_schema}.mimir_raw_articles a
        LEFT JOIN {settings.mimir_schema}.mimir_sentiment_impacts si ON a.id = si.article_id
        {where_str}
        GROUP BY a.id, a.title, a.link, a.source_name, a.published_ts, a.scraped_at, a.summary
        ORDER BY 
            CASE WHEN %s = 'impact' THEN ABS(COALESCE(AVG(si.sentiment_score), 0)) ELSE 0 END DESC,
            COALESCE(a.published_ts, a.scraped_at) DESC
        LIMIT %s OFFSET %s
    """
    
    cur.execute(data_sql, params + [sort, limit, offset])
    results = cur.fetchall()
    cur.close()
    conn.close()
    
    pages = (total_count + limit - 1) // limit if total_count > 0 else 1
    
    return {
        "total": total_count,
        "page": page,
        "limit": limit,
        "pages": pages,
        "articles": [
            {
                "id": r.get("id"),
                "title": r.get("title"),
                "link": r.get("link"),
                "source_name": r.get("source_name"),
                "published_ts": r.get("published_ts"),
                "summary": r.get("summary"),
                "impacts": r.get("impacts") or [],
                "sentiment_score": float(r.get("sentiment_score")) if r.get("sentiment_score") is not None else None
            }
            for r in results
        ]
    }


@router.get("/articles/sources")
async def get_article_sources():
    """Get all unique news sources."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    cur.execute(f"""
        SELECT DISTINCT source_name 
        FROM {settings.mimir_schema}.mimir_raw_articles 
        WHERE source_name IS NOT NULL AND source_name != ''
        ORDER BY source_name ASC
    """)
    results = cur.fetchall()
    cur.close()
    conn.close()
    
    return [r.get("source_name") for r in results]


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