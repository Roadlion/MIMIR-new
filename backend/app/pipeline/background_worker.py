# backend/app/pipeline/background_worker.py
import os
import sys
import hashlib
import time
import asyncio
import subprocess
import threading
import yfinance as yf
from yfinance import cache as yf_cache
from datetime import datetime, timezone, timedelta

# Disable yfinance sqlite disk cookie cache to avoid persistent 401 crumb errors
try:
    yf_cache.get_cookie_cache().dummy = True
    print("[BG_WORKER] cookie disk cache disabled (dummy=True)")
except Exception as e:
    print(f"[BG_WORKER] failed to disable cookie cache: {e}")
from concurrent.futures import ThreadPoolExecutor
from curl_cffi.requests import Session
from psycopg2.extras import execute_values
from ..database import get_db_connection
from ..config import get_settings
from ..routers.prices import DEFAULT_TICKERS
from ..scrapers.niche_sources import scrape_niche_articles
from ..analytics.guerilla_hybrid import get_hybrid_signals
from ..sentiment.deepseek_client import DeepSeekSentiment

settings = get_settings()

# Thread-local curl_cffi sessions — one per thread, reused across ticker fetches
_tls = threading.local()


def _get_tls_session():
    if not hasattr(_tls, 'session'):
        sess = Session(impersonate="chrome")
        sess.verify = False
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9"
        })
        _tls.session = sess
    return _tls.session

# Pathing for subprocess execution
ROUTER_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
PUSH_TO_DB_PATH = os.path.join(PROJECT_ROOT, "scripts", "push_to_db.py")
PIPELINE_PATH = os.path.join(PROJECT_ROOT, "scripts", "run_full_pipeline copy.py")
SCRAPE_SOCIAL_PATH = os.path.join(PROJECT_ROOT, "scripts", "scrape_social.py")

PRICE_FETCH_PATH = os.path.join(PROJECT_ROOT, "scripts", "run_price_fetch.py")

def run_price_fetch_cycle():
    """Runs the price fetcher as a separate subprocess to avoid GIL contention."""
    print(f"[BG_WORKER] Spawning price fetch subprocess at {datetime.now()}")
    env = _subprocess_env()
    try:
        res = subprocess.run([sys.executable, PRICE_FETCH_PATH], env=env, cwd=PROJECT_ROOT,
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode != 0:
            print(f"[BG_WORKER] Price fetch subprocess failed: {res.stderr}")
        else:
            print("[BG_WORKER] Price fetch subprocess completed successfully.")
    except Exception as e:
        print(f"[BG_WORKER] Error spawning price fetch subprocess: {e}")

def _subprocess_env():
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_scrape_cycle():
    """Lightweight: scrapes fresh articles and social chatter. No LLM calls."""
    print(f"[BG_WORKER] Starting scrape cycle at {datetime.now()}")
    env = _subprocess_env()

    # 1. Scrape articles (RSS + NewsAPI + GNews)
    try:
        print("[BG_WORKER] Executing news scraper (push_to_db.py)...")
        res = subprocess.run([sys.executable, PUSH_TO_DB_PATH], env=env, cwd=PROJECT_ROOT,
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode != 0:
            print(f"[BG_WORKER] News scraper failed: {res.stderr}")
        else:
            print("[BG_WORKER] News scraping completed successfully.")
    except Exception as e:
        print(f"[BG_WORKER] Error running news scraper: {e}")

    # 2. Social sentiment scraper (Reddit RSS + DeepSeek)
    try:
        print("[BG_WORKER] Executing social sentiment scraper (scrape_social.py)...")
        res = subprocess.run([sys.executable, SCRAPE_SOCIAL_PATH], env=env, cwd=PROJECT_ROOT,
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode != 0:
            print(f"[BG_WORKER] Social scraper failed: {res.stderr}")
        else:
            print("[BG_WORKER] Social scraping completed successfully.")
    except Exception as e:
        print(f"[BG_WORKER] Error running social scraper: {e}")

    print(f"[BG_WORKER] Scrape cycle completed.")


def run_sentiment_cycle():
    """Heavy: LLM sentiment pipeline, niche scan, relationship graph. Runs less often."""
    print(f"[BG_WORKER] Starting sentiment pipeline at {datetime.now()}")
    env = _subprocess_env()

    # 1. Score pending articles via DeepSeek (the expensive part)
    try:
        print("[BG_WORKER] Executing sentiment pipeline (run_full_pipeline copy.py)...")
        res = subprocess.run([sys.executable, PIPELINE_PATH], env=env, cwd=PROJECT_ROOT,
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode != 0:
            print(f"[BG_WORKER] Sentiment pipeline failed: {res.stderr}")
        else:
            print("[BG_WORKER] Sentiment pipeline completed successfully.")
    except Exception as e:
        print(f"[BG_WORKER] Error running sentiment pipeline: {e}")

    # 2. Guerilla Quant niche scan
    try:
        run_niche_scan()
    except Exception as e:
        print(f"[BG_WORKER] Error running niche scan: {e}")

    # 3. Refresh relationship graph (for spillover engine)
    try:
        from backend.app.sentiment.relationship_graph import refresh_relationship_graph
        n = refresh_relationship_graph()
        if n > 0:
            print(f"[BG_WORKER] Relationship graph refreshed: {n} edges")
    except Exception as e:
        print(f"[BG_WORKER] Relationship graph refresh skipped: {e}")

    print(f"[BG_WORKER] Sentiment pipeline completed.")


# ponytail: kept for backward compat — redirects to split cycles
def run_news_and_sentiment_cycle():
    """DEPRECATED: use run_scrape_cycle() + run_sentiment_cycle() instead."""
    run_scrape_cycle()
    run_sentiment_cycle()

def run_niche_scan():
    """
    Scrape niche market articles, insert into raw_articles, score via DeepSeek,
    then compute and persist pair signals for Guerilla Quant.
    """
    print(f"[BG_WORKER] Starting Guerilla Quant niche scan at {datetime.now()}")
    articles = scrape_niche_articles(sample_size=5)
    if not articles:
        print("[BG_WORKER] No niche articles scraped, skipping.")
        return

    analyzer = DeepSeekSentiment()
    scored_items = []

    for art in articles:
        title = (art.get("title") or "")[:500]
        summary = (art.get("summary") or "")[:2000]
        title_hash = hashlib.md5(title.lower().encode()).hexdigest()
        source_type = art.get("source_type", "niche")

        # Generate unique link and url_hash to avoid constraint violations
        link = art.get("link") or f"niche://{source_type}/{title_hash}"
        url_hash = hashlib.md5(link.encode()).hexdigest()

        # Parse published date
        pub_raw = art.get("published_raw", "")
        pub_ts = None
        if pub_raw:
            try:
                import dateutil.parser
                tzinfos = {
                    "EST": -18000, "EDT": -14400,
                    "CST": -21600, "CDT": -18000,
                    "MST": -25200, "MDT": -21600,
                    "PST": -28800, "PDT": -25200,
                    "UTC": 0, "GMT": 0, "BST": 3600,
                    "CET": 3600, "CEST": 7200
                }
                pub_ts = dateutil.parser.parse(pub_raw, tzinfos=tzinfos)
                if pub_ts.tzinfo is None:
                    pub_ts = pub_ts.replace(tzinfo=timezone.utc)
            except Exception:
                pub_ts = datetime.now(timezone.utc)
        else:
            pub_ts = datetime.now(timezone.utc)

        # Score immediately via DeepSeek in-memory (network call)
        assets = []
        scoring_status = 'pending'
        try:
            result = analyzer.score_article_with_assets(title, summary, force_relevance=True)
            assets = result.get("assets", [])
            scoring_status = 'scored' if assets else 'empty'
        except Exception as e:
            print(f"[BG_WORKER] Niche scoring error for title '{title[:40]}...': {e}")
            scoring_status = 'failed'

        scored_items.append({
            "source_name": f"niche-{source_type}",
            "feed_url": "",
            "title": title,
            "link": link,
            "published_raw": pub_raw,
            "published_ts": pub_ts,
            "summary": summary,
            "url_hash": url_hash,
            "title_hash": title_hash,
            "scoring_status": scoring_status,
            "assets": assets
        })

    # Now open connection ONLY to perform bulk insertion
    conn = get_db_connection()
    cur = conn.cursor()
    inserted = 0
    try:
        for item in scored_items:
            # Insert article
            cur.execute(f"""
                INSERT INTO {settings.mimir_schema}.mimir_raw_articles
                    (source_name, feed_url, title, link, published_raw, published_ts, summary, url_hash, title_hash, scoring_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (title_hash) DO NOTHING
                RETURNING id
            """, (
                item["source_name"], item["feed_url"], item["title"], item["link"],
                item["published_raw"], item["published_ts"], item["summary"],
                item["url_hash"], item["title_hash"], item["scoring_status"]
            ))
            row = cur.fetchone()
            if row:
                article_id = row[0]
                assets = item["assets"]
                if assets:
                    impact_rows = []
                    for asset in assets:
                        ticker = asset.get("ticker")
                        if not ticker:
                            continue
                        impact_rows.append((
                            article_id,
                            asset.get("asset_name", ""),
                            asset.get("asset_category", ""),
                            asset.get("sub_category"),
                            asset.get("country"),
                            asset.get("region"),
                            asset.get("sentiment_score", 0.0),
                            asset.get("confidence", 0.0),
                            asset.get("direction", "neutral"),
                            asset.get("magnitude", "MEDIUM"),
                            asset.get("reasoning", ""),
                            ticker,
                            asset.get("policy_signal"),
                        ))
                    if impact_rows:
                        insert_sql = f"""
                            INSERT INTO {settings.mimir_schema}.mimir_sentiment_impacts
                                (article_id, asset_name, asset_category, asset_sub_category, country, region,
                                 sentiment_score, confidence, direction, magnitude, reasoning, ticker, policy_signal)
                            VALUES %s ON CONFLICT (article_id, asset_name) DO NOTHING
                        """
                        execute_values(cur, insert_sql, impact_rows)
                inserted += 1
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[BG_WORKER] Database error during niche scan insertion: {e}")
    finally:
        cur.close()
        conn.close()

    # Compute and persist pair signals
    try:
        opportunities = get_hybrid_signals()
        print(f"[BG_WORKER] Niche scan complete: {inserted} articles inserted, {len(opportunities)} pair signals generated.")
    except Exception as e:
        print(f"[BG_WORKER] Error computing niche pair signals: {e}")

async def start_price_loop():
    """5-minute async loop for fetching 1-minute prices."""
    while True:
        try:
            await asyncio.to_thread(run_price_fetch_cycle)
        except Exception as e:
            print(f"[BG_WORKER] Error in price loop: {e}")
        await asyncio.sleep(300)  # every 5 minutes


async def start_scrape_loop():
    """5-minute async loop for scraping fresh articles (no heavy LLM work)."""
    while True:
        try:
            await asyncio.to_thread(run_scrape_cycle)
        except Exception as e:
            print(f"[BG_WORKER] Error in scrape loop: {e}")
        await asyncio.sleep(300)  # every 5 minutes


async def start_sentiment_loop():
    """15-minute async loop for heavy sentiment pipeline (LLM calls)."""
    while True:
        try:
            await asyncio.to_thread(run_sentiment_cycle)
        except Exception as e:
            print(f"[BG_WORKER] Error in sentiment loop: {e}")
        await asyncio.sleep(900)  # every 15 minutes


# ponytail: kept for backward compat
async def start_news_loop():
    """DEPRECATED: use start_scrape_loop() + start_sentiment_loop() instead."""
    while True:
        try:
            await asyncio.to_thread(run_news_and_sentiment_cycle)
        except Exception as e:
            print(f"[BG_WORKER] Error in news loop: {e}")
        await asyncio.sleep(300)


def start_background_worker():
    """Initializes and runs the background loops in daemon threads."""
    print("[BG_WORKER] Initializing MIMIR background workers...")

    def _thread_target(loop_func):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(loop_func())

    t_price = threading.Thread(target=_thread_target, args=(start_price_loop,), daemon=True)
    t_scrape = threading.Thread(target=_thread_target, args=(start_scrape_loop,), daemon=True)
    t_sentiment = threading.Thread(target=_thread_target, args=(start_sentiment_loop,), daemon=True)

    t_price.start()
    t_scrape.start()
    t_sentiment.start()
    print("[BG_WORKER] MIMIR background threads started (price=5m, scrape=5m, sentiment=15m).")
