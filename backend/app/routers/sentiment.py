# backend/app/routers/sentiment.py
from fastapi import APIRouter, HTTPException, Query
from typing import Optional, List
from pydantic import BaseModel
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor
from ..database import get_db_connection_dict
from ..config import get_settings
import time
router = APIRouter()
settings = get_settings()

# In-memory cache for /summary endpoint: (days, region, country) -> (timestamp, response_data)
_summary_cache = {}
CACHE_TTL_SECONDS = 60


@router.get("/social/feed")
async def get_social_feed(
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    platform: Optional[str] = Query(None),
    ticker: Optional[str] = Query(None),
    search: Optional[str] = Query(None)
):
    """Fetch aggregated social media chatter with pagination and filters."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    where_clauses = []
    params = []
    
    if platform:
        where_clauses.append("platform = %s")
        params.append(platform)
        
    if ticker:
        where_clauses.append("ticker = %s")
        params.append(ticker.upper().strip())
        
    if search:
        where_clauses.append("(ticker ILIKE %s OR asset_name ILIKE %s OR summary_text ILIKE %s)")
        params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
        
    where_str = ""
    if where_clauses:
        where_str = "WHERE " + " AND ".join(where_clauses)
        
    # Count query
    count_sql = f"SELECT COUNT(*) FROM yggdrasil.mimir_social_chatter {where_str}"
    cur.execute(count_sql, params)
    total_count = cur.fetchone()["count"]
    
    # Data query
    offset = (page - 1) * limit
    data_sql = f"""
        SELECT id, platform, channel, ticker, asset_name, bucket_ts, 
               sentiment_score, confidence, post_count, engagement_score, summary_text
        FROM yggdrasil.mimir_social_chatter
        {where_str}
        ORDER BY bucket_ts DESC
        LIMIT %s OFFSET %s
    """
    cur.execute(data_sql, params + [limit, offset])
    rows = cur.fetchall()
    
    cur.close()
    conn.close()
    
    return {
        "total": total_count,
        "page": page,
        "limit": limit,
        "data": [dict(r) for r in rows]
    }


@router.get("/social/tickers")
async def get_social_tickers():
    """Get all unique tickers currently present in social chatter."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT ticker, asset_name 
        FROM yggdrasil.mimir_social_chatter 
        ORDER BY ticker
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {"tickers": [dict(r) for r in rows]}


@router.get("/summary")
async def get_sentiment_summary(
    days: int = Query(1, ge=1, le=30),
    region: Optional[str] = Query(None),
    country: Optional[str] = Query(None)
):
    """Get overall sentiment summary for the last N days, optionally filtered by region or country."""
    cache_key = (days, region, country)
    now = time.time()
    if cache_key in _summary_cache:
        ts, cached_data = _summary_cache[cache_key]
        if now - ts < CACHE_TTL_SECONDS:
            return cached_data

    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    # 1. Main summary stats query
    where_clauses = ["a.published_ts > NOW() - INTERVAL '%s days'"]
    params = [days]
    
    if country:
        where_clauses.append("si.country = %s")
        params.append(country)
    elif region:
        where_clauses.append("si.region = %s")
        params.append(region)
        
    where_str = " AND ".join(where_clauses)
    join_type = "JOIN" if (region or country) else "LEFT JOIN"
    
    query1 = f"""
        SELECT 
            COUNT(DISTINCT a.id) AS total_articles,
            AVG(si.sentiment_score) AS avg_sentiment,
            COUNT(si.article_id) FILTER (WHERE si.direction = 'bullish') AS bullish_count,
            COUNT(si.article_id) FILTER (WHERE si.direction = 'bearish') AS bearish_count,
            COUNT(si.article_id) FILTER (WHERE si.direction = 'neutral') AS neutral_count
        FROM {settings.mimir_schema}.mimir_raw_articles a
        {join_type} {settings.mimir_schema}.mimir_sentiment_impacts si ON a.id = si.article_id
        WHERE {where_str}
    """
    
    cur.execute(query1, tuple(params))
    result = cur.fetchone()
    
    # 2. Top mover query (Optimized to pre-filter by active tickers)
    filter_clause = ""
    mover_params = [days, days, days]
    
    if country:
        filter_clause = "AND si.country = %s"
        mover_params = [days, country, days, days, country]
    elif region:
        filter_clause = "AND si.region = %s"
        mover_params = [days, region, days, days, region]
        
    query2 = f"""
        WITH sentiment_24h AS (
            SELECT 
                si.asset_name,
                si.ticker,
                AVG(si.sentiment_score) AS current_sentiment,
                COUNT(DISTINCT a.id) AS article_count
            FROM {settings.mimir_schema}.mimir_raw_articles a
            JOIN {settings.mimir_schema}.mimir_sentiment_impacts si ON a.id = si.article_id
            WHERE a.published_ts > NOW() - INTERVAL '%s days'
              {filter_clause}
            GROUP BY si.asset_name, si.ticker
        ),
        active_tickers AS (
            SELECT DISTINCT ticker FROM sentiment_24h WHERE ticker IS NOT NULL
        ),
        ticker_prices AS (
            SELECT DISTINCT ON (ticker)
                ticker,
                close AS latest_price,
                timestamp AS latest_ts
            FROM {settings.mimir_schema}.mimir_hourly_ohlcv
            WHERE ticker IN (SELECT ticker FROM active_tickers)
            ORDER BY ticker, timestamp DESC
        ),
        ticker_prices_24h AS (
            SELECT DISTINCT ON (h.ticker)
                h.ticker,
                h.close AS prev_price
            FROM {settings.mimir_schema}.mimir_hourly_ohlcv h
            WHERE h.ticker IN (SELECT ticker FROM active_tickers)
              AND h.timestamp <= NOW() - INTERVAL '24 hours'
            ORDER BY h.ticker, h.timestamp DESC
        ),
        price_changes AS (
            SELECT 
                p.ticker,
                CASE 
                    WHEN p24.prev_price > 0 THEN ((p.latest_price - p24.prev_price) / p24.prev_price) * 100
                    ELSE 0.0
                END AS price_change_percent
            FROM ticker_prices p
            LEFT JOIN ticker_prices_24h p24 ON p.ticker = p24.ticker
        ),
        sentiment_prev AS (
            SELECT 
                si.asset_name,
                AVG(si.sentiment_score) AS prev_sentiment
            FROM {settings.mimir_schema}.mimir_raw_articles a
            JOIN {settings.mimir_schema}.mimir_sentiment_impacts si ON a.id = si.article_id
            WHERE a.published_ts > NOW() - INTERVAL '%s days' * 2
              AND a.published_ts <= NOW() - INTERVAL '%s days'
              {filter_clause}
            GROUP BY si.asset_name
        )
        SELECT 
            s24.asset_name,
            s24.ticker,
            s24.current_sentiment AS avg_sentiment,
            s24.article_count,
            CASE 
                WHEN sp.prev_sentiment IS NULL THEN s24.current_sentiment * 100
                WHEN abs(sp.prev_sentiment) > 0.0001 THEN ((s24.current_sentiment - sp.prev_sentiment) / abs(sp.prev_sentiment)) * 100
                ELSE (s24.current_sentiment - sp.prev_sentiment) * 100
            END AS sentiment_change_percent,
            COALESCE(pc.price_change_percent, 0.0) AS price_change_percent
        FROM sentiment_24h s24
        LEFT JOIN sentiment_prev sp ON s24.asset_name = sp.asset_name
        LEFT JOIN price_changes pc ON s24.ticker = pc.ticker
        WHERE s24.article_count >= 1
          AND s24.ticker IS NOT NULL
          AND pc.price_change_percent IS NOT NULL
        ORDER BY s24.article_count DESC, ABS(COALESCE(pc.price_change_percent, 0.0)) DESC
        LIMIT 1
    """
    
    cur.execute(query2, tuple(mover_params))
    top_mover = cur.fetchone()
    
    top_mover_sentiment_change = 0.0
    if top_mover:
        print(f"[SUMMARY] Top Mover Found ({'region='+region if region else ('country='+country if country else 'global')}): {top_mover.get('asset_name')} | Ticker: {top_mover.get('ticker')} | Avg Sentiment: {top_mover.get('avg_sentiment')} | Price Change: {top_mover.get('price_change_percent')}%")
        top_mover_sentiment_change = float(top_mover.get("sentiment_change_percent") or 0.0)
    else:
        print(f"[SUMMARY] No Top Mover Found for {'region='+region if region else ('country='+country if country else 'global')}")
        
    cur.close()
    conn.close()
    
    response_data = {
        "total_articles": result.get("total_articles", 0) if result else 0,
        "avg_sentiment": float(result.get("avg_sentiment") or 0.0) if result else 0.0,
        "bullish_count": result.get("bullish_count", 0) if result else 0,
        "bearish_count": result.get("bearish_count", 0) if result else 0,
        "neutral_count": result.get("neutral_count", 0) if result else 0,
        "top_mover_name": top_mover.get("asset_name") if top_mover else None,
        "top_mover_ticker": top_mover.get("ticker") if top_mover else None,
        "top_mover_sentiment": float(top_mover.get("avg_sentiment")) if top_mover and top_mover.get("avg_sentiment") is not None else 0.0,
        "top_mover_sentiment_change": round(top_mover_sentiment_change, 2)
    }
    
    # Store in memory cache
    _summary_cache[cache_key] = (now, response_data)
    return response_data


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


@router.get("/assets")
async def get_available_assets():
    """Get all distinct assets (name + ticker) from sentiment impacts table."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    cur.execute(f"""
        SELECT 
            asset_name,
            ticker,
            COUNT(*) AS mention_count
        FROM {settings.mimir_schema}.mimir_sentiment_impacts
        WHERE ticker IS NOT NULL AND ticker != ''
        GROUP BY asset_name, ticker
        ORDER BY mention_count DESC, asset_name ASC
    """)
    
    results = cur.fetchall()
    cur.close()
    conn.close()
    
    return {
        "assets": [
            {
                "name": r.get("asset_name"),
                "ticker": r.get("ticker"),
                "mentions": r.get("mention_count", 0)
            }
            for r in results
        ]
    }


@router.get("/ticker-sentiments")
async def get_ticker_sentiments(
    tickers: Optional[str] = Query(None),
    weighted: bool = Query(False),
    hours: int = Query(24, ge=1, le=168),
    social_half_life: float = Query(6.0),
    social_weight: float = Query(0.25),
):
    """
    Get current and previous average sentiment for each ticker.
    If weighted=true, uses confidence-weighted, time-decayed scoring
    with spillover impacts included.
    """
    if not tickers:
        return {"tickers": []}

    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    if not ticker_list:
        return {"tickers": []}

    conn = get_db_connection_dict()
    cur = conn.cursor()

    if weighted:
        # Use the weighted sentiment function (includes social and spillover)
        results = []
        for t in ticker_list:
            cur.execute(
                "SELECT * FROM yggdrasil.mimir_weighted_sentiment("
                "p_ticker := %s, p_hours_window := %s, p_half_life_hours := 12, "
                "p_include_spillover := TRUE, p_social_half_life_hours := %s, "
                "p_social_weight_multiplier := %s)",
                (t, hours, social_half_life, social_weight)
            )
            row = cur.fetchone()
            if row:
                results.append({
                    "ticker": row.get("ticker"),
                    "current_sentiment": float(row.get("weighted_score", 0) or 0),
                    "raw_score": float(row.get("direct_score", 0) or 0),
                    "article_count": row.get("article_count", 0),
                    "spillover_count": row.get("spillover_count", 0),
                    "avg_confidence": float(row.get("avg_confidence", 0) or 0),
                    "effective_age_hours": float(row.get("effective_age_hours", 0) or 0),
                })
            else:
                results.append({
                    "ticker": t,
                    "current_sentiment": 0.0,
                    "raw_score": 0.0,
                    "article_count": 0,
                    "spillover_count": 0,
                    "avg_confidence": 0.0,
                    "effective_age_hours": 0.0,
                })
        cur.close()
        conn.close()
        return {"tickers": results, "weighted": True}

    # --- Original AVG() logic (unchanged) ---
    cur.execute(f"""
        WITH current_sentiment AS (
            SELECT
                si.ticker,
                AVG(si.sentiment_score) AS current_sentiment
            FROM (
                SELECT si_sub.ticker, si_sub.sentiment_score, a_sub.published_ts
                FROM {settings.mimir_schema}.mimir_sentiment_impacts si_sub
                JOIN {settings.mimir_schema}.mimir_raw_articles a_sub ON a_sub.id = si_sub.article_id
                
                UNION ALL
                
                SELECT sc.ticker, sc.sentiment_score, sc.bucket_ts AS published_ts
                FROM {settings.mimir_schema}.mimir_social_chatter sc
            ) si
            WHERE si.ticker = ANY(%s)
              AND si.published_ts > NOW() - INTERVAL '24 hours'
            GROUP BY si.ticker
        ),
        prev_sentiment AS (
            SELECT
                si.ticker,
                AVG(si.sentiment_score) AS prev_sentiment
            FROM (
                SELECT si_sub.ticker, si_sub.sentiment_score, a_sub.published_ts
                FROM {settings.mimir_schema}.mimir_sentiment_impacts si_sub
                JOIN {settings.mimir_schema}.mimir_raw_articles a_sub ON a_sub.id = si_sub.article_id
                
                UNION ALL
                
                SELECT sc.ticker, sc.sentiment_score, sc.bucket_ts AS published_ts
                FROM {settings.mimir_schema}.mimir_social_chatter sc
            ) si
            WHERE si.ticker = ANY(%s)
              AND si.published_ts > NOW() - INTERVAL '48 hours'
              AND si.published_ts <= NOW() - INTERVAL '24 hours'
            GROUP BY si.ticker
        )
        SELECT
            cs.ticker,
            cs.current_sentiment,
            ps.prev_sentiment,
            CASE
                WHEN ps.prev_sentiment IS NULL OR ABS(ps.prev_sentiment) < 0.0001
                    THEN (cs.current_sentiment - COALESCE(ps.prev_sentiment, 0)) * 100
                ELSE ((cs.current_sentiment - ps.prev_sentiment) / ABS(ps.prev_sentiment)) * 100
            END AS sentiment_change_percent
        FROM current_sentiment cs
        LEFT JOIN prev_sentiment ps ON cs.ticker = ps.ticker
    """, (ticker_list, ticker_list))

    results = cur.fetchall()
    cur.close()
    conn.close()

    return {
        "tickers": [
            {
                "ticker": r.get("ticker"),
                "current_sentiment": float(r.get("current_sentiment", 0)) if r.get("current_sentiment") is not None else 0.0,
                "prev_sentiment": float(r.get("prev_sentiment", 0)) if r.get("prev_sentiment") is not None else None,
                "sentiment_change_percent": round(float(r.get("sentiment_change_percent", 0)), 2) if r.get("sentiment_change_percent") is not None else 0.0
            }
            for r in results
        ]
    }


@router.get("/regional")
async def get_regional_sentiment(days: int = Query(7, ge=1, le=90)):
    """Get average sentiment score and count for each region."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    cur.execute(f"""
        SELECT 
            si.region,
            AVG(si.sentiment_score) AS avg_sentiment,
            COUNT(DISTINCT si.article_id) AS article_count,
            COUNT(si.article_id) FILTER (WHERE si.direction = 'bullish') AS bullish_count,
            COUNT(si.article_id) FILTER (WHERE si.direction = 'bearish') AS bearish_count,
            COUNT(si.article_id) FILTER (WHERE si.direction = 'neutral') AS neutral_count
        FROM {settings.mimir_schema}.mimir_sentiment_impacts si
        JOIN {settings.mimir_schema}.mimir_raw_articles a ON a.id = si.article_id
        WHERE si.region IS NOT NULL AND si.region != '' AND si.region != 'GLOBAL'
          AND a.published_ts > NOW() - INTERVAL '%s days'
        GROUP BY si.region
    """, (days,))
    
    results = cur.fetchall()
    cur.close()
    conn.close()
    
    return {
        "regions": [
            {
                "region": r.get("region"),
                "avg_sentiment": float(r.get("avg_sentiment", 0)) if r.get("avg_sentiment") is not None else 0.0,
                "article_count": r.get("article_count", 0),
                "bullish_count": r.get("bullish_count", 0),
                "bearish_count": r.get("bearish_count", 0),
                "neutral_count": r.get("neutral_count", 0)
            }
            for r in results
        ]
    }




def clean_domain(source: str) -> str:
    if not source:
        return ""
    if "://" in source:
        source = source.split("://")[1]
    source = source.split("/")[0]
    if source.startswith("www."):
        source = source[4:]
    return source


def parse_json_response(content: str) -> dict:
    import json
    import re
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    
    json_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', content)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass
            
    json_match = re.search(r'\{[\s\S]*\}', content)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass
            
    raise ValueError("Could not parse JSON from model response")


@router.get("/market-summary")
async def get_market_summary():
    """Retrieve the latest generated market summary from the database."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT summary_data, created_at 
            FROM {settings.mimir_schema}.mimir_market_summary 
            ORDER BY created_at DESC 
            LIMIT 1
        """)
        row = cur.fetchone()
        if not row:
            return {"status": "empty", "summary": None}
        
        data = row["summary_data"]
        created_at = row["created_at"]
        data["created_at"] = created_at.isoformat()
        
        return {"status": "success", "summary": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cur.close()
        conn.close()


@router.post("/market-summary/generate")
async def generate_market_summary():
    """Generate a new market summary using DeepSeek by summarizing articles from the last 24 hours."""
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    # 1. Fetch articles from last 24 hours. Fallback up to 7 days if less than 10 articles.
    results = []
    days = 1
    while len(results) < 10 and days <= 7:
        cur.execute(f"""
            SELECT 
                a.id,
                a.title,
                a.summary,
                a.source_name,
                a.published_ts,
                ARRAY_AGG(DISTINCT si.asset_name) FILTER (WHERE si.asset_name IS NOT NULL) AS assets
            FROM {settings.mimir_schema}.mimir_raw_articles a
            LEFT JOIN {settings.mimir_schema}.mimir_sentiment_impacts si ON a.id = si.article_id
            WHERE a.published_ts > NOW() - INTERVAL '%s days'
            GROUP BY a.id, a.title, a.summary, a.source_name, a.published_ts
            ORDER BY MAX(CASE WHEN si.magnitude = 'HIGH' THEN 3 WHEN si.magnitude = 'MEDIUM' THEN 2 ELSE 1 END) DESC, MAX(si.confidence) DESC, a.published_ts DESC
            LIMIT 80
        """, (days,))
        results = cur.fetchall()
        days += 1
        
    if not results:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="No articles found in the last 7 days to generate summary.")
        
    all_sources = [r["source_name"] for r in results if r.get("source_name")]
    unique_sources = []
    for s in all_sources:
        dom = clean_domain(s)
        if dom and dom not in unique_sources:
            unique_sources.append(dom)
            
    # Count all distinct sources in the database over the queried timeframe
    cur.execute(f"""
        SELECT COUNT(DISTINCT source_name) AS total_sources
        FROM {settings.mimir_schema}.mimir_raw_articles
        WHERE published_ts > NOW() - INTERVAL '%s days'
    """, (days - 1,))
    sources_row = cur.fetchone()
    total_sources_count = sources_row["total_sources"] if sources_row else len(unique_sources)
            
    # 2. Build prompt
    articles_text = ""
    for idx, art in enumerate(results):
        assets_str = ", ".join(art['assets']) if art.get('assets') else "None"
        summary_text = art['summary'] or "No summary available."
        articles_text += f"Article #{idx+1}:\n"
        articles_text += f"Title: {art['title']}\n"
        articles_text += f"Source: {art['source_name']}\n"
        articles_text += f"Assets Affected: {assets_str}\n"
        articles_text += f"Summary: {summary_text}\n\n"
        
    prompt = f"""You are MIMIR's lead macro analyst.
Analyze the following latest financial news articles and market sentiment data from the past day:

{articles_text}

Generate a "Market Summary" comprising exactly 6 main items/stories that constitute the main "story" of the last 24 hours.

For each of the 6 items, provide:
1. "title": A concise, punchy title/headline summarizing the event (under 15 words). Include movement percentages or figures if mentioned in the articles (e.g. 'Crude Oil Slides -3.05% on Iran Sanctions Waiver and Hormuz Progress').
2. "content": A detailed, analytical paragraph (4-6 sentences) explaining the macroeconomic context, specific drivers, key figures/statistics, and market implications of this event based on the provided articles. Provide in-depth analysis instead of a brief summary. Do not hallucinate figures.

Output your response as a valid JSON object with the following structure:
{{
  "stories": [
    {{
      "title": "...",
      "content": "..."
    }},
    ... (exactly 6 stories)
  ]
}}
Ensure the output is valid JSON, containing only the JSON structure. Do not include markdown code block formatting like ```json or any introductory/concluding text.
"""

    if not settings.deepseek_api_key:
        cur.close()
        conn.close()
        raise HTTPException(status_code=500, detail="DEEPSEEK_API_KEY is not set.")
        
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": 'deepseek-v4-pro',
        "messages": [
            {
                "role": "system",
                "content": "You are a financial news intelligence analyst. You output JSON only."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"}
    }
    
    try:
        resp = requests.post(
            f"{settings.deepseek_base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=60,
            verify=False
        )
        resp.raise_for_status()
        resp_data = resp.json()
        model_content = resp_data["choices"][0]["message"]["content"]
        
        parsed_summary = parse_json_response(model_content)
        stories = parsed_summary.get("stories", [])
        if not isinstance(stories, list) or len(stories) == 0:
            raise ValueError("Invalid stories format returned by model")
            
        stories = stories[:6]
        
        summary_payload = {
            "sources_count": max(total_sources_count, len(unique_sources)),
            "sources": unique_sources[:15],
            "stories": stories
        }
        
        import json as json_lib
        cur.execute(f"""
            INSERT INTO {settings.mimir_schema}.mimir_market_summary (summary_data)
            VALUES (%s)
            RETURNING created_at
        """, (json_lib.dumps(summary_payload),))
        conn.commit()
        
        row = cur.fetchone()
        created_at = row["created_at"]
        summary_payload["created_at"] = created_at.isoformat()
        
        return {"status": "success", "summary": summary_payload}
        
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Generation failed: {str(e)}")
    finally:
        cur.close()
        conn.close()


@router.get("/upcoming-events")
async def get_upcoming_events():
    """Retrieve the list of upcoming market events."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT id, event_title, event_description, event_time, event_category, importance, source_article_id
            FROM {settings.mimir_schema}.mimir_upcoming_events
            ORDER BY event_time ASC
        """)
        results = cur.fetchall()
        
        events = []
        for r in results:
            events.append({
                "id": r["id"],
                "title": r["event_title"],
                "description": r["event_description"],
                "event_time": r["event_time"].isoformat(),
                "category": r["event_category"],
                "importance": r["importance"],
                "source_article_id": r["source_article_id"]
            })
            
        return {"status": "success", "events": events}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cur.close()
        conn.close()


@router.post("/upcoming-events/generate")
async def generate_upcoming_events():
    """Scan recent articles and generate upcoming events using DeepSeek."""
    import requests
    import urllib3
    from datetime import datetime, timezone
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        cur.execute(f"""
            SELECT id, title, summary, source_name, published_ts
            FROM {settings.mimir_schema}.mimir_raw_articles
            WHERE published_ts > NOW() - INTERVAL '3 days'
              AND (
                  title ~* 'upcoming|scheduled|expected|expects|IPO|FOMC|announcement|meeting|summit|negotiation|decision|release'
                  OR summary ~* 'upcoming|scheduled|expected|expects|IPO|FOMC|announcement|meeting|summit|negotiation|decision|release'
              )
            ORDER BY published_ts DESC
            LIMIT 60
        """)
        results = cur.fetchall()
        
        if len(results) < 5:
            cur.execute(f"""
                SELECT id, title, summary, source_name, published_ts
                FROM {settings.mimir_schema}.mimir_raw_articles
                WHERE published_ts > NOW() - INTERVAL '7 days'
                  AND (
                      title ~* 'upcoming|scheduled|expected|expects|IPO|FOMC|announcement|meeting|summit|negotiation|decision|release'
                      OR summary ~* 'upcoming|scheduled|expected|expects|IPO|FOMC|announcement|meeting|summit|negotiation|decision|release'
                  )
                ORDER BY published_ts DESC
                LIMIT 60
            """)
            results = cur.fetchall()
            
        if not results:
            return {"status": "success", "events": [], "message": "No relevant forward-looking articles found."}
            
        articles_text = ""
        for idx, art in enumerate(results):
            summary_text = art['summary'] or "No summary available."
            articles_text += f"Article #{idx+1} (ID: {art['id']}):\n"
            articles_text += f"Title: {art['title']}\n"
            articles_text += f"Summary: {summary_text}\n\n"
            
        current_date = datetime.now(timezone.utc)
        current_date_str = current_date.strftime("%B %d, %Y")
        
        prompt = f"""Analyze the provided financial news articles to extract a list of 4 to 6 UPCOMING major scheduled events, key decisions, corporate milestones (IPOs, earnings), or geopolitical events mentioned.

Today's date is {current_date_str}. All extracted events MUST have their date set in the FUTURE (greater than {current_date_str}).

CRITICAL REQUIREMENT:
You MUST return at least 4 events. If the articles do not specify an exact date, you MUST estimate a logical date and time in late June or July 2026 based on the context of the upcoming event (e.g. if an article talks about SpaceX/OpenAI IPOs, set it to July 15, 2026; if it talks about Best Buy management meetings or Goldman Sachs earnings, set it to mid-July; if it talks about US-Iran peace talks or MSCI Indonesia standing, set it to late June 2026). Do not output an empty list!

For each event:
1. "title": Punchy, descriptive title (under 10 words, e.g. 'SpaceX & OpenAI IPO Pipeline Catalyst', 'Goldman Sachs Q2 Earnings Release').
2. "description": A short paragraph (2-3 sentences) explaining what the event is and why it will shake up the stock market or the world.
3. "event_time": The estimated future date in ISO 8601 UTC format (e.g. '2026-07-15T13:30:00Z'). Must be after today ({current_date_str}).
4. "category": POLICY, CORPORATE, GEOPOLITICAL, or ECONOMIC.
5. "importance": CRITICAL, HIGH, or MEDIUM.
6. "source_article_id": The ID of the article mentioning the event.

Articles to analyze:
{articles_text}

Output your response as a valid JSON object in this exact format:
{{
  "events": [
    {{
      "title": "Event Title",
      "description": "Event Description",
      "event_time": "2026-07-15T13:30:00Z",
      "category": "CORPORATE",
      "importance": "HIGH",
      "source_article_id": 123
    }}
  ]
}}
Ensure the output is valid JSON, containing only the JSON structure.
"""

        headers = {
            "Authorization": f"Bearer {settings.deepseek_api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": settings.deepseek_model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a macroeconomic events parser. You output JSON only. You must extract events and estimate dates."
                },
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"}
        }
        
        resp = requests.post(
            f"{settings.deepseek_base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=60,
            verify=False
        )
        resp.raise_for_status()
        resp_data = resp.json()
        model_content = resp_data["choices"][0]["message"]["content"]
        
        parsed_data = parse_json_response(model_content)
        events = parsed_data.get("events", [])
        if not isinstance(events, list):
            events = []
            
        inserted_events = []
        for event in events[:6]:
            try:
                event_time_str = event["event_time"]
                if not event.get("title") or not event_time_str:
                    continue
                
                # Check for duplicates on the same day (case-insensitive title and date comparison)
                cur.execute(f"""
                    SELECT id FROM {settings.mimir_schema}.mimir_upcoming_events
                    WHERE LOWER(TRIM(event_title)) = LOWER(TRIM(%s))
                      AND event_time::date = %s::date
                """, (event["title"], event_time_str))
                
                if cur.fetchone():
                    # Event already exists on this day, skip insertion
                    continue
                
                cur.execute(f"""
                    INSERT INTO {settings.mimir_schema}.mimir_upcoming_events 
                    (event_title, event_description, event_time, event_category, importance, source_article_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    event["title"],
                    event.get("description", ""),
                    event_time_str,
                    event.get("category", "OTHER"),
                    event.get("importance", "MEDIUM"),
                    event.get("source_article_id")
                ))
                row_id = cur.fetchone()["id"]
                event["id"] = row_id
                inserted_events.append(event)
            except Exception as ex:
                print(f"Skipping invalid event record: {event}. Error: {ex}")
                continue
                
        conn.commit()
        
        # Fetch all events to return to the frontend calendar
        cur.execute(f"""
            SELECT id, event_title, event_description, event_time, event_category, importance, source_article_id
            FROM {settings.mimir_schema}.mimir_upcoming_events
            ORDER BY event_time ASC
        """)
        results = cur.fetchall()
        all_events = []
        for r in results:
            all_events.append({
                "id": r["id"],
                "title": r["event_title"],
                "description": r["event_description"],
                "event_time": r["event_time"].isoformat(),
                "category": r["event_category"],
                "importance": r["importance"],
                "source_article_id": r["source_article_id"]
            })
            
        return {"status": "success", "events": all_events}
        
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Events generation failed: {str(e)}")
    finally:
        cur.close()
        conn.close()


class EventCreate(BaseModel):
    title: str
    description: Optional[str] = ""
    event_time: str
    category: Optional[str] = "OTHER"
    importance: Optional[str] = "MEDIUM"


@router.post("/upcoming-events")
async def create_upcoming_event(event: EventCreate):
    """Manually insert a new calendar event."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        # Check for duplicates on the same day (case-insensitive title and date comparison)
        cur.execute(f"""
            SELECT id FROM {settings.mimir_schema}.mimir_upcoming_events
            WHERE LOWER(TRIM(event_title)) = LOWER(TRIM(%s))
              AND event_time::date = %s::date
        """, (event.title, event.event_time))
        
        if cur.fetchone():
            raise HTTPException(status_code=400, detail="An event with this title already exists on this day.")
            
        cur.execute(f"""
            INSERT INTO {settings.mimir_schema}.mimir_upcoming_events 
            (event_title, event_description, event_time, event_category, importance)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """, (
            event.title,
            event.description,
            event.event_time,
            event.category,
            event.importance
        ))
        conn.commit()
        
        # Return all events
        cur.execute(f"""
            SELECT id, event_title, event_description, event_time, event_category, importance, source_article_id
            FROM {settings.mimir_schema}.mimir_upcoming_events
            ORDER BY event_time ASC
        """)
        results = cur.fetchall()
        events = []
        for r in results:
            events.append({
                "id": r["id"],
                "title": r["event_title"],
                "description": r["event_description"],
                "event_time": r["event_time"].isoformat(),
                "category": r["event_category"],
                "importance": r["importance"],
                "source_article_id": r["source_article_id"]
            })
        return {"status": "success", "events": events}
    except HTTPException as he:
        raise he
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cur.close()
        conn.close()


@router.delete("/upcoming-events/{event_id}")
async def delete_upcoming_event(event_id: int):
    """Delete a calendar event by ID."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        # Check if it exists
        cur.execute(f"SELECT id FROM {settings.mimir_schema}.mimir_upcoming_events WHERE id = %s", (event_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Event not found.")
            
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_upcoming_events WHERE id = %s", (event_id,))
        conn.commit()
        
        # Return all events
        cur.execute(f"""
            SELECT id, event_title, event_description, event_time, event_category, importance, source_article_id
            FROM {settings.mimir_schema}.mimir_upcoming_events
            ORDER BY event_time ASC
        """)
        results = cur.fetchall()
        events = []
        for r in results:
            events.append({
                "id": r["id"],
                "title": r["event_title"],
                "description": r["event_description"],
                "event_time": r["event_time"].isoformat(),
                "category": r["event_category"],
                "importance": r["importance"],
                "source_article_id": r["source_article_id"]
            })
        return {"status": "success", "events": events}
    except HTTPException as he:
        raise he
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cur.close()
        conn.close()


@router.get("/countries")
async def get_countries_sentiment(
    days: int = Query(7, ge=1, le=90),
    region: Optional[str] = Query(None)
):
    """Get average sentiment score for each country, optionally filtered by region."""
    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        where_clauses = ["si.country IS NOT NULL", "si.country != ''", "a.published_ts > NOW() - INTERVAL '%s days'"]
        params = [days]
        if region:
            where_clauses.append("si.region = %s")
            params.append(region)
            
        where_str = " AND ".join(where_clauses)
        
        cur.execute(f"""
            SELECT 
                si.country,
                AVG(si.sentiment_score) AS avg_sentiment,
                COUNT(DISTINCT si.article_id) AS article_count
            FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON a.id = si.article_id
            WHERE {where_str}
            GROUP BY si.country
        """, tuple(params))
        
        results = cur.fetchall()
        return {
            "countries": [
                {
                    "country": r["country"].upper(),
                    "avg_sentiment": float(r["avg_sentiment"]) if r["avg_sentiment"] is not None else 0.0,
                    "article_count": r["article_count"]
                }
                for r in results
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        cur.close()
        conn.close()


COUNTRY_CB_MAP = {
    "US": "Federal Reserve",
    "EU": "ECB", "DE": "ECB", "FR": "ECB", "IT": "ECB", "ES": "ECB",
    "JP": "BOJ",
    "CN": "PBOC",
    "GB": "BOE"
}

COUNTRY_BOND_MAP = {
    "US": "^TNX",
    "GB": "BG07.L",
    "JP": "JP10YT=RR",
    "DE": "DE10YT=RR",
    "FR": "FR10YT=RR",
    "IT": "IT10YT=RR",
    "ES": "ES10YT=RR",
    "IN": "IN10YT=RR",
    "CN": "CN10YT=RR",
    "KR": "KR10YT=RR",
    "TH": "TH10YT=RR",
    "AU": "AU10YT=RR",
    "CA": "CA10YT=RR",
    "BR": "BR10YT=RR"
}

SECTOR_TICKERS = {
    "Technology": "XLK",
    "Energy": "XLE",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Communication Services": "XLC",
    "Industrials": "XLI",
    "Financial Services": "XLF",
    "Utilities": "XLU",
    "Basic Materials": "XLB",
    "Real Estate": "XLRE",
    "Healthcare": "XLV"
}

COUNTRY_SECTOR_TICKERS = {
    "US": {
        "Technology": "XLK",
        "Energy": "XLE",
        "Consumer Cyclical": "XLY",
        "Consumer Defensive": "XLP",
        "Communication Services": "XLC",
        "Industrials": "XLI",
        "Financial Services": "XLF",
        "Utilities": "XLU",
        "Basic Materials": "XLB",
        "Real Estate": "XLRE",
        "Healthcare": "XLV"
    },
    "JP": {
        "Technology": "1618.T",
        "Energy": "1619.T",
        "Consumer Cyclical": "1622.T",
        "Consumer Defensive": "1625.T",
        "Communication Services": "1626.T",
        "Industrials": "1620.T",
        "Financial Services": "1615.T",
        "Utilities": "1627.T",
        "Basic Materials": "1617.T",
        "Real Estate": "1628.T",
        "Healthcare": "1621.T"
    },
    "EU": {
        "Technology": "EXV3.DE",
        "Energy": "EXV1.DE",
        "Consumer Cyclical": "EXV10.DE",
        "Consumer Defensive": "EXV7.DE",
        "Communication Services": "EXV12.DE",
        "Industrials": "EXV2.DE",
        "Financial Services": "EXV5.DE",
        "Utilities": "EXV9.DE",
        "Basic Materials": "EXV6.DE",
        "Real Estate": "EXV11.DE",
        "Healthcare": "EXV4.DE"
    },
    "DE": "EU", "FR": "EU", "IT": "EU", "ES": "EU",
    "GB": {
        "Technology": "IUIT.L",
        "Energy": "IOGP.L",
        "Consumer Cyclical": "EXV10.DE",
        "Consumer Defensive": "EXV7.DE",
        "Communication Services": "EXV12.DE",
        "Industrials": "EXV2.DE",
        "Financial Services": "EXV5.DE",
        "Utilities": "EXV9.DE",
        "Basic Materials": "EXV6.DE",
        "Real Estate": "EXV11.DE",
        "Healthcare": "EXV4.DE"
    },
    "CN": {
        "Technology": "3033.HK",
        "Energy": "3027.HK",
        "Consumer Cyclical": "CHIQ",
        "Consumer Defensive": "CHIS",
        "Communication Services": "KWEB",
        "Industrials": "3005.HK",
        "Financial Services": "CHIX",
        "Utilities": "3049.HK",
        "Basic Materials": "CHIM",
        "Real Estate": "3101.HK",
        "Healthcare": "KURE"
    },
    "KR": {
        "Technology": "102110.KS",
        "Energy": "117460.KS",
        "Consumer Cyclical": "091170.KS",
        "Consumer Defensive": "211900.KS",
        "Communication Services": "266370.KS",
        "Industrials": "117600.KS",
        "Financial Services": "091180.KS",
        "Utilities": "091160.KS",
        "Basic Materials": "117460.KS",
        "Real Estate": "091170.KS",
        "Healthcare": "091190.KS"
    },
    "IN": {
        "Technology": "NETFIT.NS",
        "Energy": "SETFNIFENG.NS",
        "Consumer Cyclical": "NETFAUTO.NS",
        "Consumer Defensive": "NETFFMCG.NS",
        "Communication Services": "NETFIT.NS",
        "Industrials": "SETFNIFIND.NS",
        "Financial Services": "SETFNIFBK.NS",
        "Utilities": "NETFUTI.NS",
        "Basic Materials": "NETFMAT.NS",
        "Real Estate": "NETFMAT.NS",
        "Healthcare": "NETFPHARM.NS"
    },
    "TH": {
        "Technology": "ICT.BK",
        "Energy": "ENERG.BK",
        "Consumer Cyclical": "COMM.BK",
        "Consumer Defensive": "FOOD.BK",
        "Communication Services": "ICT.BK",
        "Industrials": "CONMAT.BK",
        "Financial Services": "BANK.BK",
        "Utilities": "ENERG.BK",
        "Basic Materials": "PETRO.BK",
        "Real Estate": "PROP.BK",
        "Healthcare": "HELTH.BK"
    }
}

SECTOR_NORM_MAP = {
    "TECHNOLOGY": "Technology",
    "ENERGY": "Energy",
    "CONSUMER_CYCLICAL": "Consumer Cyclical",
    "CONSUMER_DEFENSIVE": "Consumer Defensive",
    "COMMUNICATION_SERVICES": "Communication Services",
    "INDUSTRIALS": "Industrials",
    "FINANCIAL_SERVICES": "Financial Services",
    "UTILITIES": "Utilities",
    "BASIC_MATERIALS": "Basic Materials",
    "REAL_ESTATE": "Real Estate",
    "HEALTHCARE": "Healthcare"
}

def normalize_sector_name(name: str) -> Optional[str]:
    if not name:
        return None
    name_upper = name.upper()
    if "TECH" in name_upper: return "Technology"
    if "ENERGY" in name_upper: return "Energy"
    if "CYCLICAL" in name_upper or "DISCRETIONARY" in name_upper: return "Consumer Cyclical"
    if "DEFENSIVE" in name_upper or "STAPLES" in name_upper: return "Consumer Defensive"
    if "COMMUNICATION" in name_upper: return "Communication Services"
    if "INDUSTRIAL" in name_upper: return "Industrials"
    if "FINANCIAL" in name_upper: return "Financial Services"
    if "UTILITIES" in name_upper: return "Utilities"
    if "MATERIAL" in name_upper or "BASIC" in name_upper: return "Basic Materials"
    if "REAL ESTATE" in name_upper: return "Real Estate"
    if "HEALTHCARE" in name_upper: return "Healthcare"
    return None

COUNTRY_INDEX_MAP = {
    "US": "SPY",
    "JP": "^N225",
    "CN": "000300.SS",
    "KR": "^KS11",
    "TH": "^SET50.BK",
    "GB": "^FTSE",
    "DE": "^GDAXI",
    "FR": "^FCHI",
    "IT": "FTSEMIB.MI",
    "ES": "^IBEX",
    "EU": "^STOXX50E",
    "IN": "^NSEI",
    "CA": "^GSPTSE",
    "AU": "^AXJO",
    "BR": "^BVSP"
}

REGION_REPRESENTATIVE_COUNTRY = {
    "NA": "US",
    "EU": "EU",
    "APAC": "JP",
    "ASEAN": "TH",
    "LATAM": "BR",
    "AFRICA": "ZA",
    "MENA": "SA"
}

COUNTRY_CURRENCY_MAP = {
    "US": "DX-Y.NYB",
    "GB": "GBPUSD=X",
    "JP": "USDJPY=X",
    "CN": "USDCNY=X",
    "EU": "EURUSD=X", "DE": "EURUSD=X", "FR": "EURUSD=X", "IT": "EURUSD=X", "ES": "EURUSD=X",
    "TH": "USDTHB=X",
    "AU": "AUDUSD=X",
    "CA": "USDCAD=X",
    "KR": "USDKRW=X",
    "SG": "USDSGD=X",
    "IN": "USDINR=X",
    "BR": "USDBRL=X",
    "ZA": "USDZAR=X"
}

REGION_CURRENCY_MAP = {
    "NA": "DX-Y.NYB",
    "EU": "EURUSD=X",
    "APAC": "USDJPY=X",
    "ASEAN": "USDTHB=X",
    "LATAM": "USDBRL=X",
    "AFRICA": "USDZAR=X",
    "MENA": "USDTRY=X"
}

REGION_INDEX_MAP = {
    "NA": "SPY",
    "EU": "^STOXX50E",
    "APAC": "^N225",
    "ASEAN": "^SET50.BK",
    "LATAM": "^BVSP",
    "AFRICA": "^GSPC",
    "MENA": "^GSPC"
}

def get_region_for_country(country_code: str) -> Optional[str]:
    country_to_region = {
        "US": "NA", "CA": "NA", "MX": "NA", "GL": "NA",
        "GB": "EU", "DE": "EU", "FR": "EU", "IT": "EU", "ES": "EU", "CH": "EU",
        "NL": "EU", "BE": "EU", "AT": "EU", "DK": "EU", "FI": "EU", "SE": "EU",
        "JP": "APAC", "CN": "APAC", "KR": "APAC", "IN": "APAC", "AU": "APAC", "NZ": "APAC",
        "SG": "ASEAN", "TH": "ASEAN", "MY": "ASEAN", "ID": "ASEAN", "PH": "ASEAN", "VN": "ASEAN",
        "BR": "LATAM", "AR": "LATAM", "CL": "LATAM", "CO": "LATAM",
        "SA": "MENA", "IL": "MENA", "TR": "MENA", "AE": "MENA",
        "ZA": "AFRICA", "KE": "AFRICA", "NG": "AFRICA"
    }
    return country_to_region.get(country_code)

@router.get("/countries/{country}/details")
async def get_country_details(country: str, days: int = Query(7, ge=1, le=90)):
    """
    Get detailed macroeconomic, central bank, bond yields, sector performance, stock index, and currency data for a country.
    """
    country_code = country.upper()
    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    try:
        # 1. Economic Sentiment
        cur.execute(f"""
            SELECT AVG(si.sentiment_score) as avg_sentiment, COUNT(DISTINCT si.article_id) as count
            FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON a.id = si.article_id
            WHERE si.country = %s AND si.asset_category = 'ECONOMY'
              AND a.published_ts > NOW() - INTERVAL '%s days'
        """, (country_code, days))
        eco_res = cur.fetchone()
        economy_sentiment = {
            "avg_sentiment": float(eco_res["avg_sentiment"]) if eco_res and eco_res["avg_sentiment"] is not None else 0.0,
            "count": eco_res["count"] if eco_res else 0
        }
        
        # 2. Central Bank Stance
        cb_name = COUNTRY_CB_MAP.get(country_code)
        cb_sentiment = 0.0
        cb_count = 0
        hawkish_count = 0
        dovish_count = 0
        
        cb_where = "si.country = %s"
        cb_params = [country_code, days]
        if cb_name:
            cb_where = "(si.country = %s OR si.asset_name = %s)"
            cb_params = [country_code, cb_name, days]
            
        cur.execute(f"""
            SELECT 
                AVG(si.sentiment_score) as avg_sentiment,
                COUNT(*) as count,
                SUM(CASE WHEN si.policy_signal = 'hawkish' THEN 1 ELSE 0 END) as hawkish_count,
                SUM(CASE WHEN si.policy_signal = 'dovish' THEN 1 ELSE 0 END) as dovish_count
            FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON a.id = si.article_id
            WHERE {cb_where} AND si.asset_category = 'POLICY' AND si.asset_sub_category = 'CENTRAL_BANK'
              AND a.published_ts > NOW() - INTERVAL '%s days'
        """, tuple(cb_params))
        cb_res = cur.fetchone()
        
        if cb_res and cb_res["count"] > 0:
            cb_sentiment = float(cb_res["avg_sentiment"]) if cb_res["avg_sentiment"] is not None else 0.0
            cb_count = cb_res["count"]
            hawkish_count = int(cb_res["hawkish_count"]) if cb_res["hawkish_count"] is not None else 0
            dovish_count = int(cb_res["dovish_count"]) if cb_res["dovish_count"] is not None else 0
            
        stance = "neutral"
        if hawkish_count > dovish_count:
            stance = "hawkish"
        elif dovish_count > hawkish_count:
            stance = "dovish"
            
        central_bank = {
            "name": cb_name or f"{country_code} Central Bank",
            "avg_sentiment": cb_sentiment,
            "count": cb_count,
            "hawkish_count": hawkish_count,
            "dovish_count": dovish_count,
            "stance": stance
        }

        # 3. Setup pricing queries (bond, index, currency, sectors) in a single bulk request
        bond_ticker = COUNTRY_BOND_MAP.get(country_code)
        index_ticker = COUNTRY_INDEX_MAP.get(country_code)
        currency_ticker = COUNTRY_CURRENCY_MAP.get(country_code)
        
        target_map = COUNTRY_SECTOR_TICKERS.get(country_code, COUNTRY_SECTOR_TICKERS["US"])
        if isinstance(target_map, str):
            target_map = COUNTRY_SECTOR_TICKERS[target_map]
            
        sector_list = list(SECTOR_TICKERS.keys())
        target_tickers = [target_map.get(name, SECTOR_TICKERS[name]) for name in sector_list]
        us_tickers = list(SECTOR_TICKERS.values())
        all_sector_tickers = list(set(target_tickers + us_tickers))
        
        # Combine all tickers for a single bulk query
        price_query_tickers = [t for t in [bond_ticker, index_ticker, currency_ticker] if t] + all_sector_tickers
        
        from .prices import get_ticker_changes
        price_changes_res = await get_ticker_changes(tickers=",".join(price_query_tickers))
        tickers_list = price_changes_res.get("tickers", []) if price_changes_res else []
        price_changes_map = {item["ticker"]: item for item in tickers_list}
        
        # A. Bond Yield
        bond_data = {"ticker": bond_ticker, "yield": None, "change_percent": None}
        if bond_ticker and bond_ticker in price_changes_map:
            bond_data["yield"] = price_changes_map[bond_ticker]["current_price"]
            bond_data["change_percent"] = price_changes_map[bond_ticker]["change_percent"]
            
        # B. Stock Index
        index_data = {"ticker": index_ticker, "price": None, "change_percent": None}
        if index_ticker and index_ticker in price_changes_map:
            index_data["price"] = price_changes_map[index_ticker]["current_price"]
            index_data["change_percent"] = price_changes_map[index_ticker]["change_percent"]
            
        # C. Currency
        currency_data = {"ticker": currency_ticker, "price": None, "change_percent": None}
        if currency_ticker and currency_ticker in price_changes_map:
            currency_data["price"] = price_changes_map[currency_ticker]["current_price"]
            currency_data["change_percent"] = price_changes_map[currency_ticker]["change_percent"]

        # 4. Equity Sectors Sentiments
        region_code = get_region_for_country(country_code)
        
        async def fetch_sector_sentiments(filter_col: Optional[str], filter_val: Optional[str]):
            where_clause = ""
            params = [days]
            if filter_col:
                where_clause = f"si.{filter_col} = %s AND"
                params = [filter_val, days]
                
            cur.execute(f"""
                SELECT 
                    si.asset_name,
                    si.asset_category,
                    si.asset_sub_category,
                    AVG(si.sentiment_score) as avg_sentiment
                FROM {settings.mimir_schema}.mimir_sentiment_impacts si
                JOIN {settings.mimir_schema}.mimir_raw_articles a ON a.id = si.article_id
                WHERE {where_clause} (
                    si.asset_category = 'SECTOR' OR 
                    (si.asset_category = 'EQUITY' AND si.asset_sub_category IS NOT NULL)
                )
                  AND a.published_ts > NOW() - INTERVAL '%s days'
                GROUP BY si.asset_name, si.asset_category, si.asset_sub_category
            """, tuple(params))
            rows = cur.fetchall()
            
            s_map = {}
            for r in rows:
                norm_name = None
                if r["asset_category"] == "EQUITY" and r["asset_sub_category"]:
                    norm_name = SECTOR_NORM_MAP.get(r["asset_sub_category"].strip().upper())
                else:
                    norm_name = normalize_sector_name(r["asset_name"])
                    
                if norm_name:
                    if norm_name not in s_map:
                        s_map[norm_name] = []
                    s_map[norm_name].append(float(r["avg_sentiment"]))
                    
            return {k: sum(v)/len(v) for k, v in s_map.items()}

        country_sent_map = await fetch_sector_sentiments("country", country_code)
        region_sent_map = await fetch_sector_sentiments("region", region_code) if region_code else {}
        global_sent_map = await fetch_sector_sentiments(None, None)
        
        sectors = []
        for name in sector_list:
            ticker = target_map.get(name, SECTOR_TICKERS[name])
            p_data = price_changes_map.get(ticker, {})
            price = p_data.get("current_price")
            change_percent = p_data.get("change_percent")
            
            if price is None or change_percent is None:
                us_ticker = SECTOR_TICKERS[name]
                us_data = price_changes_map.get(us_ticker, {})
                price = us_data.get("current_price")
                change_percent = us_data.get("change_percent")
                ticker = us_ticker
                
            avg_sent = 0.0
            if name in country_sent_map:
                avg_sent = country_sent_map[name]
            elif name in region_sent_map:
                avg_sent = region_sent_map[name] * 0.9
            elif name in global_sent_map:
                avg_sent = global_sent_map[name] * 0.8
                
            sectors.append({
                "sector": name,
                "ticker": ticker,
                "price": round(price, 2) if price is not None else None,
                "change_percent": round(change_percent, 2) if change_percent is not None else None,
                "sentiment_score": round(avg_sent, 2)
            })

        return {
            "country": country_code,
            "economy_sentiment": economy_sentiment,
            "central_bank": central_bank,
            "bond_yield": bond_data,
            "stock_index": index_data,
            "currency": currency_data,
            "sectors": sectors
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error in country details: {str(e)}")
    finally:
        cur.close()
        conn.close()


@router.get("/regions/{region}/details")
async def get_region_details(region: str, days: int = Query(7, ge=1, le=90)):
    """
    Get detailed macroeconomic, central bank, bond yields, sector performance, stock index, and currency data for a region.
    """
    region_code = region.upper()
    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    try:
        # 1. Economic Sentiment
        cur.execute(f"""
            SELECT AVG(si.sentiment_score) as avg_sentiment, COUNT(DISTINCT si.article_id) as count
            FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON a.id = si.article_id
            WHERE si.region = %s AND si.asset_category = 'ECONOMY'
              AND a.published_ts > NOW() - INTERVAL '%s days'
        """, (region_code, days))
        eco_res = cur.fetchone()
        economy_sentiment = {
            "avg_sentiment": float(eco_res["avg_sentiment"]) if eco_res and eco_res["avg_sentiment"] is not None else 0.0,
            "count": eco_res["count"] if eco_res else 0
        }
        
        # Representative country code for Central Bank and Bond Yield
        rep_country = REGION_REPRESENTATIVE_COUNTRY.get(region_code, "US")
        
        # 2. Central Bank Stance (Representative of region)
        cb_name = COUNTRY_CB_MAP.get(rep_country)
        cb_sentiment = 0.0
        cb_count = 0
        hawkish_count = 0
        dovish_count = 0
        
        cb_where = "si.country = %s"
        cb_params = [rep_country, days]
        if cb_name:
            cb_where = "(si.country = %s OR si.asset_name = %s)"
            cb_params = [rep_country, cb_name, days]
            
        cur.execute(f"""
            SELECT 
                AVG(si.sentiment_score) as avg_sentiment,
                COUNT(*) as count,
                SUM(CASE WHEN si.policy_signal = 'hawkish' THEN 1 ELSE 0 END) as hawkish_count,
                SUM(CASE WHEN si.policy_signal = 'dovish' THEN 1 ELSE 0 END) as dovish_count
            FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON a.id = si.article_id
            WHERE {cb_where} AND si.asset_category = 'POLICY' AND si.asset_sub_category = 'CENTRAL_BANK'
              AND a.published_ts > NOW() - INTERVAL '%s days'
        """, tuple(cb_params))
        cb_res = cur.fetchone()
        
        if cb_res and cb_res["count"] > 0:
            cb_sentiment = float(cb_res["avg_sentiment"]) if cb_res["avg_sentiment"] is not None else 0.0
            cb_count = cb_res["count"]
            hawkish_count = int(cb_res["hawkish_count"]) if cb_res["hawkish_count"] is not None else 0
            dovish_count = int(cb_res["dovish_count"]) if cb_res["dovish_count"] is not None else 0
            
        stance = "neutral"
        if hawkish_count > dovish_count:
            stance = "hawkish"
        elif dovish_count > hawkish_count:
            stance = "dovish"
            
        central_bank = {
            "name": cb_name or f"{region_code} Main Central Bank",
            "avg_sentiment": cb_sentiment,
            "count": cb_count,
            "hawkish_count": hawkish_count,
            "dovish_count": dovish_count,
            "stance": stance
        }
        
        # 3. Setup pricing queries (bond, index, currency, sectors) in a single bulk request
        bond_ticker = COUNTRY_BOND_MAP.get(rep_country)
        index_ticker = REGION_INDEX_MAP.get(region_code)
        currency_ticker = REGION_CURRENCY_MAP.get(region_code)
        
        target_map = COUNTRY_SECTOR_TICKERS.get(rep_country, COUNTRY_SECTOR_TICKERS["US"])
        if isinstance(target_map, str):
            target_map = COUNTRY_SECTOR_TICKERS[target_map]
            
        sector_list = list(SECTOR_TICKERS.keys())
        target_tickers = [target_map.get(name, SECTOR_TICKERS[name]) for name in sector_list]
        us_tickers = list(SECTOR_TICKERS.values())
        all_sector_tickers = list(set(target_tickers + us_tickers))
        
        price_query_tickers = [t for t in [bond_ticker, index_ticker, currency_ticker] if t] + all_sector_tickers
        
        from .prices import get_ticker_changes
        price_changes_res = await get_ticker_changes(tickers=",".join(price_query_tickers))
        tickers_list = price_changes_res.get("tickers", []) if price_changes_res else []
        price_changes_map = {item["ticker"]: item for item in tickers_list}
        
        # A. Bond Yield
        bond_data = {"ticker": bond_ticker, "yield": None, "change_percent": None}
        if bond_ticker and bond_ticker in price_changes_map:
            bond_data["yield"] = price_changes_map[bond_ticker]["current_price"]
            bond_data["change_percent"] = price_changes_map[bond_ticker]["change_percent"]
            
        # B. Stock Index
        index_data = {"ticker": index_ticker, "price": None, "change_percent": None}
        if index_ticker and index_ticker in price_changes_map:
            index_data["price"] = price_changes_map[index_ticker]["current_price"]
            index_data["change_percent"] = price_changes_map[index_ticker]["change_percent"]
            
        # C. Currency
        currency_data = {"ticker": currency_ticker, "price": None, "change_percent": None}
        if currency_ticker and currency_ticker in price_changes_map:
            currency_data["price"] = price_changes_map[currency_ticker]["current_price"]
            currency_data["change_percent"] = price_changes_map[currency_ticker]["change_percent"]
            
        # 4. Sector performance (Regional aggregate)
        cur.execute(f"""
            SELECT 
                si.asset_name,
                si.asset_category,
                si.asset_sub_category,
                AVG(si.sentiment_score) as avg_sentiment
            FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON a.id = si.article_id
            WHERE si.region = %s AND (
                si.asset_category = 'SECTOR' OR 
                (si.asset_category = 'EQUITY' AND si.asset_sub_category IS NOT NULL)
            )
              AND a.published_ts > NOW() - INTERVAL '%s days'
            GROUP BY si.asset_name, si.asset_category, si.asset_sub_category
        """, (region_code, days))
        rows = cur.fetchall()
        
        s_map = {}
        for r in rows:
            norm_name = None
            if r["asset_category"] == "EQUITY" and r["asset_sub_category"]:
                norm_name = SECTOR_NORM_MAP.get(r["asset_sub_category"].strip().upper())
            else:
                norm_name = normalize_sector_name(r["asset_name"])
                
            if norm_name:
                if norm_name not in s_map:
                    s_map[norm_name] = []
                s_map[norm_name].append(float(r["avg_sentiment"]))
        region_sent_map = {k: sum(v)/len(v) for k, v in s_map.items()}
        
        sectors = []
        for name in sector_list:
            ticker = target_map.get(name, SECTOR_TICKERS[name])
            p_data = price_changes_map.get(ticker, {})
            price = p_data.get("current_price")
            change_percent = p_data.get("change_percent")
            
            if price is None or change_percent is None:
                us_ticker = SECTOR_TICKERS[name]
                us_data = price_changes_map.get(us_ticker, {})
                price = us_data.get("current_price")
                change_percent = us_data.get("change_percent")
                ticker = us_ticker
                
            avg_sent = region_sent_map.get(name, 0.0)
            
            sectors.append({
                "sector": name,
                "ticker": ticker,
                "price": round(price, 2) if price is not None else None,
                "change_percent": round(change_percent, 2) if change_percent is not None else None,
                "sentiment_score": round(avg_sent, 2)
            })
            
        return {
            "region": region_code,
            "economy_sentiment": economy_sentiment,
            "central_bank": central_bank,
            "bond_yield": bond_data,
            "stock_index": index_data,
            "currency": currency_data,
            "sectors": sectors
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error in region details: {str(e)}")
    finally:
        cur.close()
        conn.close()


@router.post("/countries/{target}/briefing")
async def generate_target_briefing(target: str, days: int = Query(3, ge=1, le=14)):
    """
    Generate a dynamic geopolitical/geoeconomic briefing for a country, region, or global
    by analyzing recent news articles and sentiment impact reasonings.
    """
    target_code = target.upper()
    is_region = target_code in ["NA", "EU", "APAC", "ASEAN", "LATAM", "AFRICA", "MENA"]
    is_global = target_code == "GLOBAL"
    
    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    if is_global:
        where_clause = "1=1"
        query_params = (days,)
    else:
        where_clause = "si.region = %s" if is_region else "si.country = %s"
        query_params = (target_code, days)
        
    cur.execute(f"""
        SELECT DISTINCT
            a.title,
            a.published_ts,
            si.asset_name,
            si.sentiment_score,
            si.reasoning
        FROM {settings.mimir_schema}.mimir_raw_articles a
        JOIN {settings.mimir_schema}.mimir_sentiment_impacts si ON a.id = si.article_id
        WHERE {where_clause} AND si.reasoning IS NOT NULL AND si.reasoning != ''
          AND a.published_ts > NOW() - INTERVAL '%s days'
        ORDER BY a.published_ts DESC
        LIMIT 40
    """, query_params)
    
    results = cur.fetchall()
    cur.close()
    conn.close()
    
    if not results:
        return {
            "status": "empty",
            "briefing": f"No recent news flow or sentiment impacts recorded for {target_code} in the last {days} days to generate a briefing."
        }
        
    context_text = ""
    for idx, r in enumerate(results):
        context_text += f"Article #{idx+1}: {r['title']}\n"
        context_text += f"Asset Affected: {r['asset_name']} (Sentiment: {r['sentiment_score']})\n"
        context_text += f"Analyst Reasoning: {r['reasoning']}\n\n"
        
    prompt = f"""You are MIMIR's Chief Geopolitical Quant. 
Analyze the following recent market intelligence and analyst reasonings for {target_code} (representing a {'region' if is_region else 'country'}):

{context_text}

Generate a concise "Geopolitical and Geoeconomic Situation Briefing" for {target_code}.
Structure your briefing as exactly 3-4 bullet points (using markdown '-').
Cover the following areas:
1. Central Bank & Monetary Policy: Stance, direction, or sentiment based on the news (hawkish/dovish indicators).
2. Stock Market & Currency: Market performance, index movements, or currency trends.
3. Major Macro Risks or Drivers: Growth outlook, trade, geopolitical tensions, or commodities impacts.

Be extremely quantitative, direct, and clinical (Bloomberg Terminal style). Avoid pleasantries, introductory sentences, or filler words. Stick strictly to the facts and figures present in the articles.
Keep the total response under 150 words.
"""

    if not settings.deepseek_api_key:
        raise HTTPException(status_code=500, detail="DEEPSEEK_API_KEY is not set.")
        
    import requests
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json"
    }
    
    model_name = settings.deepseek_model
    if "pro" in model_name:
        model_name = "deepseek-v4-pro"
        
    payload = {
        "model": model_name,
        "messages": [
            {
                "role": "system",
                "content": "You are a senior macroeconomic analyst. You write dense, highly informative Bloomberg-style summaries."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "temperature": 0.2
    }
    
    try:
        api_url = f"{settings.deepseek_base_url}/chat/completions"
        response = requests.post(api_url, json=payload, headers=headers, verify=False, timeout=30)
        response.raise_for_status()
        res_data = response.json()
        briefing = res_data["choices"][0]["message"]["content"].strip()
        return {"status": "success", "briefing": briefing}
    except Exception as e:
        print(f"Error generating briefing: {e}")
        raise HTTPException(status_code=500, detail=f"LLM generation failed: {str(e)}")


@router.get("/spillover-log/{ticker}")
async def get_spillover_log(
    ticker: str,
    hours: int = Query(48, ge=1, le=168),
):
    """
    Show spillover events that contributed to a ticker's sentiment.
    Lists indirect (is_spillover=TRUE) impacts with their source articles.
    """
    conn = get_db_connection_dict()
    cur = conn.cursor()

    cur.execute(f"""
        SELECT
            si.sentiment_score,
            si.reasoning,
            si.confidence,
            si.spillover_source_asset,
            si.created_at,
            a.title,
            a.published_ts
        FROM {settings.mimir_schema}.mimir_sentiment_impacts si
        JOIN {settings.mimir_schema}.mimir_raw_articles a ON a.id = si.article_id
        WHERE UPPER(si.ticker) = UPPER(%s)
          AND si.is_spillover = TRUE
          AND a.published_ts > NOW() - INTERVAL '%s hours'
        ORDER BY a.published_ts DESC
        LIMIT 50
    """, (ticker, hours))

    results = cur.fetchall()
    cur.close()
    conn.close()

    return {
        "ticker": ticker.upper(),
        "spillover_events": [
            {
                "sentiment_score": float(r.get("sentiment_score", 0)),
                "confidence": float(r.get("confidence", 0)) if r.get("confidence") else None,
                "source_asset": r.get("spillover_source_asset"),
                "reasoning": r.get("reasoning"),
                "article_title": r.get("title"),
                "published_ts": r.get("published_ts").isoformat() if r.get("published_ts") else None,
            }
            for r in results
        ],
        "count": len(results),
    }
