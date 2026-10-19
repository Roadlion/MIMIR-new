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
from ..analytics.stat_arb_apu import StatArbAPU
from ..sentiment.deepseek_client import DeepSeekSentiment
from ..services.process_manager import apply_low_process_priority, trim_process_memory

settings = get_settings()


def is_us_market_active() -> bool:
    """Returns True if US equity markets are in pre-market, regular, or post-market hours (07:00 - 20:00 ET, Mon-Fri)."""
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo('America/New_York'))
        if now_et.weekday() >= 5:  # Saturday or Sunday
            return False
        time_str = now_et.strftime('%H:%M')
        return "07:00" <= time_str <= "20:00"
    except Exception:
        return True

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
PIPELINE_PATH = os.path.join(PROJECT_ROOT, "scripts", "run_full_pipeline.py")
SCRAPE_SOCIAL_PATH = os.path.join(PROJECT_ROOT, "scripts", "scrape_social.py")
SCRAPE_TWITTER_PATH = os.path.join(PROJECT_ROOT, "scripts", "scrape_twitter.py")
FETCH_FUNDAMENTALS_PATH = os.path.join(PROJECT_ROOT, "scripts", "fetch_fundamentals.py")

PRICE_FETCH_PATH = os.path.join(PROJECT_ROOT, "scripts", "run_price_fetch.py")

_shutdown_event = threading.Event()
_active_subprocesses = set()

def stop_background_worker():
    """Signals all background worker threads to exit and kills any active child subprocesses."""
    print("[BG_WORKER] Stopping all background workers and halting active subprocesses...")
    _shutdown_event.set()
    for proc in list(_active_subprocesses):
        try:
            proc.kill()
        except Exception:
            pass
    _active_subprocesses.clear()

async def _interruptible_sleep(seconds: int):
    """Sleeps in 1-second intervals so shutdown signals can be acted on immediately."""
    for _ in range(seconds):
        if _shutdown_event.is_set():
            break
        await asyncio.sleep(1)

def run_managed_subprocess(cmd, env=None, cwd=None):
    """Spawns a subprocess tracked in _active_subprocesses so it can be terminated on shutdown."""
    if _shutdown_event.is_set():
        return None
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        _active_subprocesses.add(proc)
        stdout, stderr = proc.communicate()
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except Exception as e:
        print(f"[BG_WORKER] Subprocess execution error: {e}")
        return None
    finally:
        if proc:
            _active_subprocesses.discard(proc)

def run_price_fetch_cycle():
    """Runs the price fetcher as a separate subprocess to avoid GIL contention."""
    if _shutdown_event.is_set():
        return
    print(f"[BG_WORKER] Spawning price fetch subprocess at {datetime.now()}")
    env = _subprocess_env()
    try:
        res = run_managed_subprocess([sys.executable, PRICE_FETCH_PATH], env=env, cwd=PROJECT_ROOT)
        if res and res.returncode != 0:
            print(f"[BG_WORKER] Price fetch subprocess failed: {res.stderr}")
        elif res:
            print("[BG_WORKER] Price fetch subprocess completed successfully.")
    except Exception as e:
        print(f"[BG_WORKER] Error spawning price fetch subprocess: {e}")

def run_fundamentals_cycle():
    """Runs the fundamentals fetcher as a separate subprocess to avoid GIL contention."""
    if _shutdown_event.is_set():
        return
    print(f"[BG_WORKER] Spawning fundamentals fetcher subprocess at {datetime.now()}")
    env = _subprocess_env()
    try:
        res = run_managed_subprocess([sys.executable, FETCH_FUNDAMENTALS_PATH], env=env, cwd=PROJECT_ROOT)
        if res and res.returncode != 0:
            print(f"[BG_WORKER] Fundamentals fetcher failed: {res.stderr}")
        elif res:
            print("[BG_WORKER] Fundamentals fetcher completed successfully.")
    except Exception as e:
        print(f"[BG_WORKER] Error spawning fundamentals fetcher: {e}")

def _subprocess_env():
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_scrape_cycle():
    """Lightweight: scrapes fresh articles and social chatter. No LLM calls."""
    print(f"[BG_WORKER] Starting scrape cycle at {datetime.now()}")
    env = _subprocess_env()

    # 0. SEC EDGAR 8-K real-time scraper (inline — fast, no subprocess needed)
    try:
        from ..scrapers.edgar_scraper import run_edgar_scrape_cycle
        edgar_count = run_edgar_scrape_cycle()
        if edgar_count > 0:
            print(f"[BG_WORKER] EDGAR: {edgar_count} new 8-K articles inserted.")
    except Exception as e:
        print(f"[BG_WORKER] EDGAR scrape error: {e}")

    try:
        print("[BG_WORKER] Executing news scraper (push_to_db.py)...")
        res = run_managed_subprocess([sys.executable, PUSH_TO_DB_PATH], env=env, cwd=PROJECT_ROOT)
        if res and res.returncode != 0:
            print(f"[BG_WORKER] News scraper failed: {res.stderr}")
        elif res:
            print("[BG_WORKER] News scraping completed successfully.")
    except Exception as e:
        print(f"[BG_WORKER] Error running news scraper: {e}")

    # 2. Social sentiment scraper (Reddit RSS + DeepSeek)
    try:
        print("[BG_WORKER] Executing social sentiment scraper (scrape_social.py)...")
        res = run_managed_subprocess([sys.executable, SCRAPE_SOCIAL_PATH], env=env, cwd=PROJECT_ROOT)
        if res and res.returncode != 0:
            print(f"[BG_WORKER] Social scraper failed: {res.stderr}")
        elif res:
            print("[BG_WORKER] Social scraping completed successfully.")
    except Exception as e:
        print(f"[BG_WORKER] Error running social scraper: {e}")

    # 3. Twitter sentiment scraper (FinTwit syndication + DeepSeek)
    try:
        print("[BG_WORKER] Executing Twitter sentiment scraper (scrape_twitter.py)...")
        res = run_managed_subprocess([sys.executable, SCRAPE_TWITTER_PATH], env=env, cwd=PROJECT_ROOT)
        if res and res.returncode != 0:
            print(f"[BG_WORKER] Twitter scraper failed: {res.stderr}")
        elif res:
            print("[BG_WORKER] Twitter scraping completed successfully.")
    except Exception as e:
        print(f"[BG_WORKER] Error running Twitter scraper: {e}")

    print(f"[BG_WORKER] Scrape cycle completed.")

    # Social surge detection runs after every scrape cycle so it catches
    # intra-day volume spikes as fresh social data arrives.
    try:
        from ..analytics.surge_detector import run_surge_detection_cycle
        surge_fired = run_surge_detection_cycle()
        if surge_fired > 0:
            print(f"[BG_WORKER] SURGE: {surge_fired} pre-FOMO BUY signals fired.")
    except Exception as e:
        print(f"[BG_WORKER] Social surge detection error: {e}")


def run_sentiment_cycle():
    """Heavy: LLM sentiment pipeline, niche scan, relationship graph. Runs less often."""
    if _shutdown_event.is_set():
        return
    print(f"[BG_WORKER] Starting sentiment pipeline at {datetime.now()}")
    env = _subprocess_env()

    # 1. Score pending articles via DeepSeek (the expensive part)
    try:
        print("[BG_WORKER] Executing sentiment pipeline (run_full_pipeline copy.py)...")
        res = run_managed_subprocess([sys.executable, PIPELINE_PATH], env=env, cwd=PROJECT_ROOT)
        if res and res.returncode != 0:
            print(f"[BG_WORKER] Sentiment pipeline failed: {res.stderr}")
        elif res:
            print("[BG_WORKER] Sentiment pipeline completed successfully.")
    except Exception as e:
        print(f"[BG_WORKER] Error running sentiment pipeline: {e}")

    # 2. Stat-Arb APU market-neutral scan
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
    then compute and persist pair signals via the Stat-Arb APU.
    """
    print(f"[BG_WORKER] Starting Stat-Arb APU market-neutral scan at {datetime.now()}")
    articles = scrape_niche_articles(sample_size=30)
    if not articles:
        print("[BG_WORKER] No niche articles scraped, skipping.")
        return

    # Check database to filter out existing articles (avoid duplicate scoring & API costs)
    conn_check = get_db_connection()
    cur_check = conn_check.cursor()
    new_articles = []
    for art in articles:
        title = (art.get("title") or "")[:500]
        title_hash = hashlib.md5(title.lower().encode()).hexdigest()
        cur_check.execute(f"SELECT EXISTS(SELECT 1 FROM {settings.mimir_schema}.mimir_raw_articles WHERE title_hash = %s)", (title_hash,))
        if not cur_check.fetchone()[0]:
            new_articles.append(art)
    cur_check.close()
    conn_check.close()

    print(f"[BG_WORKER] Scraped {len(articles)} articles total. {len(new_articles)} are new. Scoring new articles...")

    scored_items = []
    if new_articles:
        from ..sentiment.pre_scoring_filter import is_noise_or_junk, get_headline_deduplicator
        deduper = get_headline_deduplicator()
        analyzer = DeepSeekSentiment()

        for art in new_articles:
            title = (art.get("title") or "")[:500]
            summary = (art.get("summary") or "")[:2000]
            title_hash = hashlib.md5(title.lower().encode()).hexdigest()
            source_type = art.get("source_type", "niche")

            # 1. Zero-cost noise & clickbait check
            is_junk, junk_reason = is_noise_or_junk(title, summary)
            if is_junk:
                link = art.get("link") or f"niche://{source_type}/{title_hash}"
                url_hash = hashlib.md5(link.encode()).hexdigest()
                scored_items.append({
                    "source_name": f"niche-{source_type}",
                    "feed_url": "", "title": title, "link": link,
                    "published_raw": "", "published_ts": datetime.now(timezone.utc),
                    "summary": summary, "url_hash": url_hash, "title_hash": title_hash,
                    "scoring_status": "noise", "assets": []
                })
                continue

            # 2. Zero-cost deduplication check
            is_dup, _, dup_reason = deduper.is_duplicate(title)
            if is_dup:
                link = art.get("link") or f"niche://{source_type}/{title_hash}"
                url_hash = hashlib.md5(link.encode()).hexdigest()
                scored_items.append({
                    "source_name": f"niche-{source_type}",
                    "feed_url": "", "title": title, "link": link,
                    "published_raw": "", "published_ts": datetime.now(timezone.utc),
                    "summary": summary, "url_hash": url_hash, "title_hash": title_hash,
                    "scoring_status": "duplicate", "assets": []
                })
                continue

            deduper.register(title, 0)

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

            # Score only verified, clean, non-duplicate articles
            assets = []
            scoring_status = 'pending'
            try:
                result = analyzer.score_article_with_assets(title, summary, force_relevance=False)
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
        opportunities = StatArbAPU.run_master_scan()
        print(f"[BG_WORKER] Stat-Arb APU scan complete: {inserted} articles inserted, {len(opportunities)} pair signals generated.")
    except Exception as e:
        print(f"[BG_WORKER] Error computing Stat-Arb APU pair signals: {e}")

async def start_price_loop():
    """5-minute async loop for fetching 1-minute prices (throttled to 20m off-market)."""
    await asyncio.sleep(5)  # Stagger startup
    while True:
        try:
            await asyncio.to_thread(run_price_fetch_cycle)
            await asyncio.to_thread(trim_process_memory)
        except Exception as e:
            print(f"[BG_WORKER] Error in price loop: {e}")
        sleep_sec = 300 if is_us_market_active() else 1200
        await asyncio.sleep(sleep_sec)


async def start_scrape_loop():
    """5-minute async loop for scraping fresh articles (no heavy LLM work)."""
    await asyncio.sleep(15)  # Stagger startup
    while True:
        try:
            await asyncio.to_thread(run_scrape_cycle)
            await asyncio.to_thread(trim_process_memory)
        except Exception as e:
            print(f"[BG_WORKER] Error in scrape loop: {e}")
        await asyncio.sleep(300)  # every 5 minutes


async def start_sentiment_loop():
    """5-minute async loop for sentiment pipeline (smaller batches for fine velocity calculations)."""
    await asyncio.sleep(30)  # Stagger startup
    while True:
        try:
            await asyncio.to_thread(run_sentiment_cycle)
            await asyncio.to_thread(trim_process_memory)
        except Exception as e:
            print(f"[BG_WORKER] Error in sentiment loop: {e}")
        await asyncio.sleep(300)  # accelerated to 5 minutes


async def start_signal_fusion_loop():
    """10-minute async loop scanning price and sentiment exclusively through the War Rig Transmission Engine (throttled to 30m off-market)."""
    await asyncio.sleep(45)  # Stagger startup
    from ..analytics.signal_fusion import scan_all_tickers
    while True:
        try:
            print("[BG_WORKER] Starting War Rig Single-Shaft Transmission Scan...")
            await asyncio.to_thread(scan_all_tickers)
            await asyncio.to_thread(trim_process_memory)
        except Exception as e:
            print(f"[BG_WORKER] Error in War Rig Transmission loop: {e}")
        sleep_sec = 600 if is_us_market_active() else 1800
        await asyncio.sleep(sleep_sec)


async def start_fundamentals_loop():
    """12-hour async loop for fetching ticker fundamentals."""
    await asyncio.sleep(60)  # Stagger startup
    while True:
        try:
            await asyncio.to_thread(run_fundamentals_cycle)
        except Exception as e:
            print(f"[BG_WORKER] Error in fundamentals loop: {e}")
        await asyncio.sleep(43200)  # every 12 hours


async def start_earnings_calendar_loop():
    """
    24-hour async loop that refreshes the earnings calendar for all tracked tickers.
    Uses yfinance — free, no API key required.
    Runs once at startup (after 2 min delay) then every 24 hours.
    """
    await asyncio.sleep(120)  # Startup delay: let prices + DB settle first
    from ..services.earnings_calendar import refresh_earnings_calendar
    from ..routers.prices import DEFAULT_TICKERS

    while True:
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            tickers = set(DEFAULT_TICKERS)
            try:
                cur.execute(
                    f"SELECT DISTINCT ticker FROM {settings.mimir_schema}.mimir_dynamic_tickers "
                    f"WHERE ticker IS NOT NULL"
                )
                for row in cur.fetchall():
                    tickers.add(row[0].strip().upper())
            except Exception:
                pass
            cur.close()
            conn.close()

            print(f"[BG_WORKER] Refreshing earnings calendar for {len(tickers)} tickers...")
            n = await asyncio.to_thread(refresh_earnings_calendar, list(tickers))
            print(f"[BG_WORKER] Earnings calendar refresh complete: {n} dates upserted.")
        except Exception as e:
            print(f"[BG_WORKER] Error in earnings calendar loop: {e}")
        await asyncio.sleep(86400)  # every 24 hours


async def start_performance_and_learning_loop():
    """
    1-hour async loop that:
    1. Evaluates mature trade signals (every 1 hour).
    2. Runs nightly supply chain co-occurrence mining (every 24 hours).
    3. Runs weekly chain validation self-learning loop (every 7 days).
    """
    await asyncio.sleep(90)  # Stagger startup
    from ..analytics.performance_evaluator import evaluate_past_signals
    from ..sentiment.supply_chain_mapper import mine_cooccurrence_chains
    
    last_mining_time = datetime.now()
    
    while True:
        try:
            print("[BG_WORKER] Running Trade Alerts Performance Evaluation...")
            # Run evaluation (updates trade signals with PnL and successful/failed tags)
            await asyncio.to_thread(evaluate_past_signals)
            
            # Nightly co-occurrence mining
            if datetime.now() - last_mining_time >= timedelta(hours=24):
                print("[BG_WORKER] Running nightly supply chain co-occurrence mining...")
                await asyncio.to_thread(mine_cooccurrence_chains, 3)
                last_mining_time = datetime.now()
                
        except Exception as e:
            print(f"[BG_WORKER] Error in performance/learning loop: {e}")
            
        await asyncio.sleep(3600)  # every 1 hour


async def start_paper_trading_loop():
    """3-minute async loop for alert-based auto paper trading and position exit monitoring (throttled to 10m off-market)."""
    await asyncio.sleep(20)  # Stagger startup
    from ..analytics.paper_trader import auto_execute_pending_alerts, process_paper_position_exits
    while True:
        try:
            await asyncio.to_thread(auto_execute_pending_alerts)
            await asyncio.to_thread(process_paper_position_exits)
            await asyncio.to_thread(trim_process_memory)
        except Exception as e:
            print(f"[BG_WORKER] Error in paper trading loop: {e}")
        sleep_sec = 180 if is_us_market_active() else 600
        await asyncio.sleep(sleep_sec)


# ponytail: kept for backward compat
async def start_news_loop():
    """DEPRECATED: use start_scrape_loop() + start_sentiment_loop() instead."""
    while True:
        try:
            await asyncio.to_thread(run_news_and_sentiment_cycle)
        except Exception as e:
            print(f"[BG_WORKER] Error in news loop: {e}")
        await asyncio.sleep(300)


async def start_sitrep_and_macro_monitor_loop():
    """
    Async surveillance loop that:
    1. Synthesizes and updates active multi-day market narratives.
    2. Evaluates real-time macro shocks (VIX surges, 10Y/30Y yield spikes, regime flips).
    3. Schedules 09:00 ET Pre-Market Briefing and 16:30 ET Post-Market Wrap.
    4. Compiles automated 12-hour periodic Sit Reps.
    """
    await asyncio.sleep(20)  # Startup delay
    from zoneinfo import ZoneInfo
    from ..analytics.narrative_tracker import update_active_narratives
    from ..services.sitrep_service import compile_sitrep, evaluate_macro_shocks, compile_session_briefing

    last_sitrep_time = datetime.now() - timedelta(hours=12) # force immediate compile on boot
    last_premarket_date = None
    last_postmarket_date = None

    while True:
        try:
            # 1. Real-time Macro Shock Evaluation (every 10m)
            print("[BG_WORKER] Macro Shock Sentinel: evaluating volatility & yield benchmarks...")
            shocks = await asyncio.to_thread(evaluate_macro_shocks)
            if shocks:
                print(f"[BG_WORKER] Macro Shock Sentinel triggered {len(shocks)} alerts: {[s['type'] for s in shocks]}")

            # 2. Market Session Cadence Check (America/New_York)
            now_et = datetime.now(ZoneInfo('America/New_York'))
            current_date_et = now_et.date()
            time_et_str = now_et.strftime('%H:%M')
            weekday = now_et.weekday()  # Mon=0, Fri=4

            # Pre-Market Briefing: 09:00 to 09:30 ET on weekdays
            if weekday < 5 and '09:00' <= time_et_str <= '09:30' and last_premarket_date != current_date_et:
                print("[BG_WORKER] Compiling 09:00 ET Pre-Market Battle Briefing...")
                res = await asyncio.to_thread(compile_session_briefing, "PRE_MARKET")
                last_premarket_date = current_date_et
                print(f"[BG_WORKER] Pre-Market Briefing dispatched: {res.get('success')}")

            # Post-Market Wrap: 16:30 to 17:00 ET on weekdays
            if weekday < 5 and '16:30' <= time_et_str <= '17:00' and last_postmarket_date != current_date_et:
                print("[BG_WORKER] Compiling 16:30 ET Post-Market Debrief...")
                res = await asyncio.to_thread(compile_session_briefing, "POST_MARKET")
                last_postmarket_date = current_date_et
                print(f"[BG_WORKER] Post-Market Debrief dispatched: {res.get('success')}")

            # 3. Multi-day Narrative Synthesis
            print("[BG_WORKER] Synthesizing multi-day active market narratives...")
            await asyncio.to_thread(update_active_narratives)

            # 4. Periodic Sit Rep (every 12 hours)
            if datetime.now() - last_sitrep_time >= timedelta(hours=12):
                print("[BG_WORKER] Compiling automated MIMIR Situation Report (Sit Rep)...")
                await asyncio.to_thread(compile_sitrep, "PERIODIC")
                last_sitrep_time = datetime.now()
        except Exception as e:
            print(f"[BG_WORKER] Error in Sit Rep & Macro monitor loop: {e}")

        await asyncio.sleep(600)  # Check every 10 minutes


def start_background_worker():
    """Initializes and runs the background loops in daemon threads at BELOW_NORMAL CPU priority."""
    apply_low_process_priority()
    print("[BG_WORKER] Initializing MIMIR background workers (Gaming/Eco Mode Active: Below-Normal CPU Priority)...")

    def _thread_target(loop_func):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(loop_func())

    t_price       = threading.Thread(target=_thread_target, args=(start_price_loop,), daemon=True)
    t_scrape      = threading.Thread(target=_thread_target, args=(start_scrape_loop,), daemon=True)
    t_sentiment   = threading.Thread(target=_thread_target, args=(start_sentiment_loop,), daemon=True)
    t_fusion      = threading.Thread(target=_thread_target, args=(start_signal_fusion_loop,), daemon=True)
    t_fundamentals= threading.Thread(target=_thread_target, args=(start_fundamentals_loop,), daemon=True)
    t_learning    = threading.Thread(target=_thread_target, args=(start_performance_and_learning_loop,), daemon=True)
    t_paper       = threading.Thread(target=_thread_target, args=(start_paper_trading_loop,), daemon=True)
    t_earnings    = threading.Thread(target=_thread_target, args=(start_earnings_calendar_loop,), daemon=True)
    t_sitrep      = threading.Thread(target=_thread_target, args=(start_sitrep_and_macro_monitor_loop,), daemon=True)

    t_price.start()
    t_scrape.start()
    t_sentiment.start()
    t_fusion.start()
    t_fundamentals.start()
    t_learning.start()
    t_paper.start()
    t_earnings.start()
    t_sitrep.start()

    print(
        "[BG_WORKER] MIMIR background threads started:\n"
        "  price=5m | scrape=5m (incl. EDGAR 8-K + surge detect) | sentiment=15m\n"
        "  fusion=10m | fundamentals=12h | learning=1h | paper=3m | earnings-cal=24h | sitrep=30m"
    )

