# backend/app/routers/refresh.py
import os
import sys
import json
import asyncio
import queue
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from .prices import DEFAULT_TICKERS, fetch_and_cache_ticker
from ..database import get_db_connection
from ..config import get_settings
from datetime import datetime, timezone, timedelta

router = APIRouter()

ROUTER_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(ROUTER_DIR)))
PUSH_TO_DB_PATH = os.path.join(PROJECT_ROOT, "scripts", "push_to_db.py")
PIPELINE_PATH = os.path.join(PROJECT_ROOT, "scripts", "run_full_pipeline copy.py")

def safe_print(message: str):
    """Prints a message to stdout safely, handling encoding errors on Windows."""
    try:
        print(message)
    except UnicodeEncodeError:
        try:
            encoding = sys.stdout.encoding or 'utf-8'
            print(message.encode(encoding, errors='replace').decode(encoding))
        except Exception:
            pass

def safe_write(text: str):
    """Writes text to stdout safely, handling encoding errors on Windows."""
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except UnicodeEncodeError:
        try:
            encoding = sys.stdout.encoding or 'utf-8'
            sys.stdout.write(text.encode(encoding, errors='replace').decode(encoding))
            sys.stdout.flush()
        except Exception:
            pass

async def run_subprocess_sse(script_path, env):
    """Runs a subprocess inside a separate thread and streams stdout line-by-line asynchronously."""
    q = queue.Queue()
    
    def worker():
        try:
            proc = subprocess.Popen(
                [sys.executable, script_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=PROJECT_ROOT,
                bufsize=1
            )
            for line in proc.stdout:
                # line is bytes, decode explicitly
                line_str = line.decode('utf-8', errors='replace')
                q.put(('log', line_str))
            proc.wait()
            q.put(('done', proc.returncode))
        except Exception as e:
            q.put(('error', str(e)))
            
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    
    while True:
        try:
            msg_type, val = q.get_nowait()
            if msg_type == 'log':
                yield val
            elif msg_type == 'done':
                if val != 0:
                    raise Exception(f"Script failed with exit code {val}")
                break
            elif msg_type == 'error':
                raise Exception(val)
        except queue.Empty:
            await asyncio.sleep(0.05)

@router.get("/refresh/stream")
async def refresh_stream():
    async def event_generator():
        try:
            safe_print("\n" + "=" * 60)
            safe_print(">> STARTING MANUALLY TRIGGERED PIPELINE REFRESH <<")
            safe_print("=" * 60)
            
            # Step 1: Update prices (0-30%)
            yield f"data: {json.dumps({'type': 'progress', 'step': 'prices', 'percentage': 0, 'message': 'Updating prices from yfinance...'})}\n\n"
            
            # Combine DEFAULT_TICKERS with all unique tickers found in database
            tickers_to_fetch = list(DEFAULT_TICKERS)
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute("SELECT DISTINCT ticker FROM yggdrasil.mimir_sentiment_impacts WHERE ticker IS NOT NULL")
                impact_tickers = [row[0] for row in cur.fetchall()]
                cur.execute("SELECT DISTINCT ticker FROM yggdrasil.mimir_dynamic_tickers WHERE ticker IS NOT NULL")
                dynamic_tickers = [row[0] for row in cur.fetchall()]
                cur.close()
                conn.close()
                
                combined_tickers = set(tickers_to_fetch + impact_tickers + dynamic_tickers)
                cleaned = [t.strip().lstrip('$').upper() for t in combined_tickers if t]
                tickers_to_fetch = sorted(list(set(cleaned)))
                safe_print(f"[REFRESH] Combined tickers to fetch ({len(tickers_to_fetch)}): {tickers_to_fetch}")
            except Exception as dbe:
                safe_print(f"[REFRESH] Error retrieving dynamic tickers from DB: {dbe}")

            # 1. Bulk check cache freshness for all combined tickers
            settings = get_settings()
            stale_tickers = []
            try:
                conn = get_db_connection()
                cur = conn.cursor()
                cur.execute(f"""
                    SELECT ticker, MAX(scraped_at) as last_scraped 
                    FROM {settings.mimir_schema}.mimir_hourly_ohlcv 
                    WHERE ticker = ANY(%s)
                    GROUP BY ticker
                """, (tickers_to_fetch,))
                rows = cur.fetchall()
                cur.close()
                conn.close()
                
                last_scraped_map = {row[0]: row[1] for row in rows}
                now_ts = datetime.now(timezone.utc)
                
                for t in tickers_to_fetch:
                    last_scraped = last_scraped_map.get(t)
                    is_stale = False
                    if not last_scraped:
                        is_stale = True
                    else:
                        if last_scraped.tzinfo is not None:
                            if now_ts - last_scraped > timedelta(minutes=2):
                                is_stale = True
                        else:
                            if datetime.now() - last_scraped > timedelta(minutes=2):
                                is_stale = True
                    if is_stale:
                        stale_tickers.append(t)
            except Exception as e:
                safe_print(f"[REFRESH] Error checking bulk price cache: {e}")
                stale_tickers = tickers_to_fetch

            safe_print(f"[REFRESH] Out of {len(tickers_to_fetch)} tickers, {len(stale_tickers)} are stale/missing and will be fetched from yfinance.")

            if stale_tickers:
                futures = {}
                max_workers = min(len(stale_tickers), 5)
                completed_count = 0
                
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    for ticker in stale_tickers:
                        def run_fetch(t):
                            try:
                                return fetch_and_cache_ticker(t, None)
                            except Exception as ex:
                                safe_print(f"[REFRESH] Error fetching {t}: {ex}")
                                return False
                        fut = executor.submit(run_fetch, ticker)
                        futures[fut] = ticker
                    
                    for fut in as_completed(futures):
                        ticker = futures[fut]
                        completed_count += 1
                        pct = int((completed_count / len(stale_tickers)) * 30)
                        success = fut.result()
                        msg = f"Price for {ticker} updated." if success else f"Failed to update price for {ticker}."
                        yield f"data: {json.dumps({'type': 'progress', 'step': 'prices', 'percentage': pct, 'message': msg})}\n\n"
                        safe_print(f"[REFRESH] Price for {ticker} updated ({completed_count}/{len(stale_tickers)}).")
            else:
                yield f"data: {json.dumps({'type': 'progress', 'step': 'prices', 'percentage': 30, 'message': 'All prices are already up to date.'})}\n\n"
                
            yield f"data: {json.dumps({'type': 'progress', 'step': 'prices', 'percentage': 30, 'message': 'Price updates completed.'})}\n\n"
            safe_print("[REFRESH] All ticker price updates completed.")
            
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            
            # Step 2: Scrape articles (30-60%)
            yield f"data: {json.dumps({'type': 'progress', 'step': 'scraping', 'percentage': 30, 'message': 'Scraping new articles (running push_to_db.py)...'})}\n\n"
            safe_print(f"[REFRESH] Running push_to_db.py...")
            
            try:
                async for line_str in run_subprocess_sse(PUSH_TO_DB_PATH, env):
                    safe_write(line_str)
                    yield f"data: {json.dumps({'type': 'log', 'text': line_str.rstrip()})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'type': 'progress', 'step': 'error', 'percentage': 30, 'message': f'push_to_db.py failed: {str(e)}'})}\n\n"
                safe_print(f"[REFRESH] push_to_db.py failed: {str(e)}")
                return
                
            yield f"data: {json.dumps({'type': 'progress', 'step': 'scraping', 'percentage': 60, 'message': 'Article scraping completed.'})}\n\n"
            safe_print("[REFRESH] Article scraping completed.")

            # Step 3: Sentiment analysis (60-100%)
            yield f"data: {json.dumps({'type': 'progress', 'step': 'sentiment', 'percentage': 60, 'message': 'Processing sentiment pipeline (running run_full_pipeline copy.py)...'})}\n\n"
            safe_print(f"[REFRESH] Running run_full_pipeline copy.py...")
            
            try:
                async for line_str in run_subprocess_sse(PIPELINE_PATH, env):
                    safe_write(line_str)
                    yield f"data: {json.dumps({'type': 'log', 'text': line_str.rstrip()})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'type': 'progress', 'step': 'error', 'percentage': 60, 'message': f'run_full_pipeline copy.py failed: {str(e)}'})}\n\n"
                safe_print(f"[REFRESH] run_full_pipeline copy.py failed: {str(e)}")
                return
                
            yield f"data: {json.dumps({'type': 'progress', 'step': 'done', 'percentage': 100, 'message': 'Refresh complete! Valhalla has updated.'})}\n\n"
            safe_print("[REFRESH] Pipeline refresh sequence completed successfully.")
            safe_print("=" * 60 + "\n")
            
        except Exception as e:
            err_msg = f"Unexpected error during refresh: {str(e)}"
            safe_print(f"[REFRESH] {err_msg}")
            yield f"data: {json.dumps({'type': 'progress', 'step': 'error', 'percentage': 100, 'message': err_msg})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
