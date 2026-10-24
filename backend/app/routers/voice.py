# backend/app/routers/voice.py - Mimir Voice Router (Instant Voice Routing Active)
from fastapi import APIRouter, HTTPException
import json
import logging
import datetime
from typing import Dict, Any, List

from ..database import get_db_connection_dict
from ..config import get_settings
from ..sentiment.llm_client import send_chat_completion
from ..services.voice_service import generate_mimir_speech, get_custom_voice_sample_path

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api/v1/voice", tags=["voice"])

@router.get("/status")
def get_voice_status():
    """Check custom voice file status and TTS engine availability."""
    custom_sample = get_custom_voice_sample_path()
    return {
        "has_custom_voice_sample": custom_sample is not None,
        "sample_path": custom_sample,
        "default_voice": "en-GB-RyanNeural (Scottish brogue tuned)",
        "status": "ready"
    }

@router.get("/recap")
async def get_market_and_portfolio_recap():
    """
    Generates a Jarvis-style voice recap of overnight market moves and portfolio impacts,
    voiced in Mimir's authentic Scottish persona.
    """
    # 1. Fetch user portfolio holdings from DB
    portfolio_items = []
    conn = get_db_connection_dict()
    cur = conn.cursor()
    
    try:
        cur.execute(f"""
            SELECT ticker, buy_price, quantity, transaction_type, order_date
            FROM {settings.mimir_schema}.mimir_portfolio
            WHERE source IS NULL OR source = 'MANUAL' OR source = ''
        """)
        rows = cur.fetchall()
        
        # Aggregate holdings
        holdings_map = {}
        for r in rows:
            ticker = r["ticker"].upper()
            qty = float(r["quantity"])
            price = float(r["buy_price"])
            tx_type = (r.get("transaction_type") or "BUY").upper()
            
            if ticker not in holdings_map:
                holdings_map[ticker] = {"qty": 0.0, "total_cost": 0.0}
                
            if tx_type == "BUY":
                holdings_map[ticker]["qty"] += qty
                holdings_map[ticker]["total_cost"] += qty * price
            elif tx_type == "SELL":
                holdings_map[ticker]["qty"] -= qty
                
        for t, data in holdings_map.items():
            if data["qty"] > 0:
                avg_cost = data["total_cost"] / data["qty"] if data["qty"] > 0 else 0.0
                portfolio_items.append({
                    "ticker": t,
                    "quantity": round(data["qty"], 4),
                    "avg_cost": round(avg_cost, 2)
                })
    except Exception as e:
        logger.warning(f"Error reading portfolio for voice recap: {e}")
    finally:
        cur.close()
        conn.close()

    # 2. Fetch recent sentiment impacts for portfolio assets
    sentiment_highlights = []
    if portfolio_items:
        tickers_tuple = tuple(p["ticker"] for p in portfolio_items)
        conn = get_db_connection_dict()
        cur = conn.cursor()
        try:
            # Query recent sentiment impacts for these tickers
            if len(tickers_tuple) == 1:
                cur.execute(f"""
                    SELECT si.ticker, si.sentiment_score, si.reasoning, a.title, si.created_at
                    FROM {settings.mimir_schema}.mimir_sentiment_impacts si
                    LEFT JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
                    WHERE si.ticker = %s
                    ORDER BY si.created_at DESC
                    LIMIT 10
                """, (tickers_tuple[0],))
            else:
                cur.execute(f"""
                    SELECT si.ticker, si.sentiment_score, si.reasoning, a.title, si.created_at
                    FROM {settings.mimir_schema}.mimir_sentiment_impacts si
                    LEFT JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
                    WHERE si.ticker IN %s
                    ORDER BY si.created_at DESC
                    LIMIT 10
                """, (tickers_tuple,))
            s_rows = cur.fetchall()
            for sr in s_rows:
                sentiment_highlights.append({
                    "ticker": sr["ticker"],
                    "score": float(sr["sentiment_score"]) if sr.get("sentiment_score") is not None else 0.0,
                    "summary": sr.get("reasoning") or sr.get("title") or "Sentiment update"
                })
        except Exception as e:
            logger.warning(f"Error reading sentiment impacts for voice recap: {e}")
        finally:
            cur.close()
            conn.close()

    # 3. Construct LLM Context
    holdings_text = json.dumps(portfolio_items) if portfolio_items else "No current active stock positions registered."
    sentiment_text = json.dumps(sentiment_highlights) if sentiment_highlights else "No major headline spikes detected overnight."

    system_prompt = """You are MIMIR, the Smartest Man Alive, master of macroeconomic intelligence, quantitative analysis, and market strategy.
You speak with Alastair Duncan's iconic God of War persona: witty, blunt, authoritative, and fiercely loyal to your user (calling them "Brother" or "Laddie").
Your job is to deliver a concise 45-60 second spoken morning briefing on overnight market movements and how they affect the user's specific portfolio holdings.

You MUST respond strictly in valid JSON with this exact structure:
{
  "voice_script": "The exact 45-60 second spoken text in Mimir's authentic Scottish brogue tone. Use terms like 'Laddie', 'Brother', 'Mind your flank'. Keep it punchy, insightful, and focused on tactical risk.",
  "market_summary": "A 1-2 sentence executive summary of overall market tone.",
  "portfolio_digest": [
    {
      "ticker": "TICKER",
      "status": "ACCUMULATING | PANIC_OVERSOLD | ALIGNED | NEUTRAL",
      "impact": "Short 1-sentence tactical breakdown of overnight impact on this stock."
    }
  ]
}"""

    user_prompt = f"""Deliver Mimir's Market & Portfolio Briefing.

Current User Portfolio Positions:
{holdings_text}

Recent Overnight News & Sentiment Highlights:
{sentiment_text}

Provide your response as JSON."""

    # 4. Request JSON completion from DeepSeek / LLM
    try:
        raw_llm_res = send_chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
            response_format={"type": "json_object"}
        )
        parsed_res = json.loads(raw_llm_res)
        voice_script = parsed_res.get("voice_script", "Aye, Brother. Markets are turning, but we stand firm. Mind your risk.")
        market_summary = parsed_res.get("market_summary", "Overnight markets showed mixed sentiment across tech and macro sectors.")
        portfolio_digest = parsed_res.get("portfolio_digest", [])
    except Exception as e:
        logger.error(f"Failed to generate LLM voice script: {str(e)}")
        voice_script = "Well now, Brother. Market data is flowing into Valhalla as we speak. Keep an eye on your positions and mind your stop-losses today!"
        market_summary = "Market intelligence active."
        portfolio_digest = []

    # 5. Synthesize speech via voice service
    try:
        audio_url = await generate_mimir_speech(voice_script)
    except Exception as e:
        logger.error(f"Speech generation error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Speech synthesis error: {str(e)}")

    return {
        "audio_url": audio_url,
        "voice_script": voice_script,
        "market_summary": market_summary,
        "portfolio_digest": portfolio_digest,
        "created_at": datetime.datetime.now().isoformat()
    }
