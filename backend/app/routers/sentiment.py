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
    
    # Get top mover (asset with positive sentiment change and positive price change)
    cur.execute(f"""
        WITH ticker_prices AS (
            SELECT DISTINCT ON (ticker)
                ticker,
                close AS latest_price,
                timestamp AS latest_ts
            FROM {settings.mimir_schema}.mimir_hourly_ohlcv
            ORDER BY ticker, timestamp DESC
        ),
        ticker_prices_24h AS (
            SELECT DISTINCT ON (p.ticker)
                p.ticker,
                h.close AS prev_price
            FROM ticker_prices p
            JOIN {settings.mimir_schema}.mimir_hourly_ohlcv h ON p.ticker = h.ticker
            WHERE h.timestamp <= p.latest_ts - INTERVAL '24 hours'
            ORDER BY p.ticker, h.timestamp DESC
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
        sentiment_24h AS (
            SELECT 
                si.asset_name,
                si.ticker,
                AVG(si.sentiment_score) AS current_sentiment,
                COUNT(DISTINCT a.id) AS article_count
            FROM {settings.mimir_schema}.mimir_raw_articles a
            JOIN {settings.mimir_schema}.mimir_sentiment_impacts si ON a.id = si.article_id
            WHERE a.published_ts > NOW() - INTERVAL '%s days'
            GROUP BY si.asset_name, si.ticker
        ),
        sentiment_prev AS (
            SELECT 
                si.asset_name,
                AVG(si.sentiment_score) AS prev_sentiment
            FROM {settings.mimir_schema}.mimir_raw_articles a
            JOIN {settings.mimir_schema}.mimir_sentiment_impacts si ON a.id = si.article_id
            WHERE a.published_ts > NOW() - INTERVAL '%s days' * 2
              AND a.published_ts <= NOW() - INTERVAL '%s days'
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
          AND (
              -- positive sentiment change
              (
                  CASE 
                      WHEN sp.prev_sentiment IS NULL THEN s24.current_sentiment * 100
                      WHEN abs(sp.prev_sentiment) > 0.0001 THEN ((s24.current_sentiment - sp.prev_sentiment) / abs(sp.prev_sentiment)) * 100
                      ELSE (s24.current_sentiment - sp.prev_sentiment) * 100
                  END
              ) > 0
          )
          AND pc.price_change_percent > 0
        ORDER BY ABS(s24.current_sentiment) DESC, s24.article_count DESC
        LIMIT 1
    """, (days, days, days))
    
    top_mover = cur.fetchone()
    
    top_mover_sentiment_change = 0.0
    if top_mover:
        print(f"[SUMMARY] Top Mover Found: {top_mover.get('asset_name')} | Ticker: {top_mover.get('ticker')} | Avg Sentiment: {top_mover.get('avg_sentiment')} | Price Change: {top_mover.get('price_change_percent')}%")
        top_mover_sentiment_change = float(top_mover.get("sentiment_change_percent") or 0.0)
    else:
        print("[SUMMARY] No Top Mover Found matching criteria.")
        
    cur.close()
    conn.close()
    
    return {
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
async def get_ticker_sentiments(tickers: Optional[str] = Query(None)):
    """
    Get current (last 24h) and previous (24-48h) average sentiment for each ticker,
    along with the percentage change.
    """
    if not tickers:
        return {"tickers": []}

    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    if not ticker_list:
        return {"tickers": []}

    conn = get_db_connection_dict()
    cur = conn.cursor()

    cur.execute(f"""
        WITH current_sentiment AS (
            SELECT
                si.ticker,
                AVG(si.sentiment_score) AS current_sentiment
            FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON a.id = si.article_id
            WHERE si.ticker = ANY(%s)
              AND a.published_ts > NOW() - INTERVAL '24 hours'
            GROUP BY si.ticker
        ),
        prev_sentiment AS (
            SELECT
                si.ticker,
                AVG(si.sentiment_score) AS prev_sentiment
            FROM {settings.mimir_schema}.mimir_sentiment_impacts si
            JOIN {settings.mimir_schema}.mimir_raw_articles a ON a.id = si.article_id
            WHERE si.ticker = ANY(%s)
              AND a.published_ts > NOW() - INTERVAL '48 hours'
              AND a.published_ts <= NOW() - INTERVAL '24 hours'
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



