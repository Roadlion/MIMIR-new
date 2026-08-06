# backend/app/pipeline/sentiment_processor.py
import psycopg2
from psycopg2.extras import execute_values
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional
from backend.app.database import get_db_connection
from backend.app.sentiment.deepseek_client import DeepSeekSentiment
from backend.app.sentiment.asset_mapper import resolve_ticker, resolve_country_code, resolve_region

# Spillover integration (lazy init — ponytail: avoids circular import at module level)
_spillover_engine = None
_thematic_detector = None


def _get_spillover_engine():
    global _spillover_engine
    if _spillover_engine is None:
        from backend.app.pipeline.spillover_engine import SpilloverEngine
        _spillover_engine = SpilloverEngine()
    return _spillover_engine


def _get_thematic_detector():
    global _thematic_detector
    if _thematic_detector is None:
        from backend.app.sentiment.thematic_detector import ThematicDetector
        _thematic_detector = ThematicDetector()
    return _thematic_detector


def process_single_article(article_id: int, title: str, summary: str) -> int:
    """
    Process one article: score it, insert impacts, update status.
    Returns:
        >0 : number of impacts inserted
         0 : no assets found (status set to 'empty')
        -1 : error occurred (status set to 'pending' for retry)
    """
    client = DeepSeekSentiment()
    conn = None
    cur = None
    try:
        # --- Score the article ---
        result = client.score_article_with_assets(title, summary or "")
        assets = result.get("assets", [])

        # --- Connect to DB (separate connection per thread) ---
        conn = get_db_connection()
        cur = conn.cursor()

        if not assets:
            # No assets found → mark as 'empty' (skip forever)
            cur.execute("""
                UPDATE yggdrasil.mimir_raw_articles 
                SET scoring_status = 'empty' 
                WHERE id = %s
            """, (article_id,))
            conn.commit()
            print(f"  [Thread] Article {article_id}: No assets -> marked empty")
            return 0

        # --- Build impacts list ---
        impacts = []
        for asset in assets:
            asset_name = asset.get("asset_name", "").strip()
            if not asset_name:
                continue

            ticker, _ = resolve_ticker(asset_name)
            country = asset.get("country")
            if country:
                country = resolve_country_code(country) or country
            else:
                country = resolve_country_code(asset_name)
            # Enforce strict mapping of region by resolving it from the country code first
            resolved_reg = resolve_region(country) if country else None
            region = resolved_reg or asset.get("region")
            from datetime import datetime, timezone
            now_ts = datetime.now(timezone.utc)

            impacts.append((
                article_id,
                asset_name,
                asset.get("asset_category", "UNKNOWN"),
                asset.get("sub_category"),
                country,
                region,
                asset.get("sentiment_score", 0.0),
                asset.get("confidence", 0.5),
                asset.get("direction", "neutral"),
                asset.get("magnitude", "MEDIUM"),
                asset.get("reasoning", ""),
                ticker,
                asset.get("policy_signal"),
                False,  # is_spillover
                None,   # spillover_source_article_id
                None,   # spillover_source_asset
                now_ts, # activation_date (direct impact is immediate)
            ))

        if not impacts:
            # Should not happen if assets non-empty, but just in case
            cur.execute("""
                UPDATE yggdrasil.mimir_raw_articles 
                SET scoring_status = 'empty' 
                WHERE id = %s
            """, (article_id,))
            conn.commit()
            return 0

        # --- Insert direct impacts ---
        sql = """
        INSERT INTO yggdrasil.mimir_sentiment_impacts (
            article_id, asset_name, asset_category, asset_sub_category,
            country, region, sentiment_score, confidence, direction,
            magnitude, reasoning, ticker, policy_signal,
            is_spillover, spillover_source_article_id, spillover_source_asset, activation_date
        ) VALUES %s
        ON CONFLICT (article_id, asset_name) DO NOTHING;
        """
        execute_values(cur, sql, impacts)
        conn.commit()
        inserted = cur.rowcount

        # --- Trigger LLM Supply Chain Discovery for high-impact headlines (|score| >= 0.7) ---
        for imp in impacts:
            score = imp[6]
            ticker_val = imp[11]
            if ticker_val and abs(score) >= 0.7:
                try:
                    from backend.app.sentiment.supply_chain_mapper import discover_llm_supply_chain
                    discover_llm_supply_chain(title, summary or "", ticker_val, score, conn=conn)
                except Exception as llm_err:
                    print(f"  [Thread] [Warning] LLM supply chain discovery error for {ticker_val}: {llm_err}")

        # --- Compute spillover impacts (graph-based + thematic) ---
        spillover_inserted = 0
        try:
            # Build asset dicts for spillover engine
            asset_dicts = []
            for imp in impacts:
                asset_dicts.append({
                    "asset_name": imp[1],
                    "asset_category": imp[2],
                    "sub_category": imp[3],
                    "country": imp[4],
                    "region": imp[5],
                    "sentiment_score": imp[6],
                    "confidence": imp[7],
                    "ticker": imp[11],
                })

            # Graph-based spillover
            engine = _get_spillover_engine()
            graph_spills = engine.run(article_id, asset_dicts, published_ts=now_ts)

            # Thematic spillover
            detector = _get_thematic_detector()
            thematic_spills = detector.compute_spillovers(
                article_id, asset_dicts, title, summary or "",
            )

            # Merge and insert
            all_spills = graph_spills + thematic_spills
            if all_spills:
                execute_values(cur, sql, all_spills)
                conn.commit()
                spillover_inserted = cur.rowcount
        except Exception as spill_err:
            print(f"  [Thread] [Warning] Spillover skipped for article {article_id}: {spill_err}")

        # --- Mark article as 'scored' ---
        cur.execute("""
            UPDATE yggdrasil.mimir_raw_articles
            SET scoring_status = 'scored'
            WHERE id = %s
        """, (article_id,))
        conn.commit()

        print(f"  [Thread] Article {article_id}: {inserted} direct + {spillover_inserted} spillover -> scored")
        return inserted + spillover_inserted

    except Exception as e:
        print(f"  [Thread] [Error] Error on article {article_id}: {e}")
        # Mark as 'pending' so it can be retried later
        if conn and cur:
            try:
                cur.execute("""
                    UPDATE yggdrasil.mimir_raw_articles 
                    SET scoring_status = 'pending' 
                    WHERE id = %s
                """, (article_id,))
                conn.commit()
            except Exception as db_err:
                print(f"  [Thread] Failed to update status for article {article_id}: {db_err}")
        return -1
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def process_article_batch(batch_articles: List[tuple]) -> int:
    """
    Process a batch of up to 5 articles in a single LLM API call.
    batch_articles: List of (article_id, title, summary)
    Returns: total number of inserted impacts.
    """
    if not batch_articles:
        return 0

    client = DeepSeekSentiment()
    art_dicts = [{"id": aid, "title": title, "summary": summary or ""} for aid, title, summary in batch_articles]
    
    # Score batch via single DeepSeek call
    batch_results = client.score_articles_batch(art_dicts)

    conn = get_db_connection()
    cur = conn.cursor()
    total_impacts_inserted = 0

    try:
        from datetime import datetime, timezone
        now_ts = datetime.now(timezone.utc)

        for aid, title, summary in batch_articles:
            res = batch_results.get(aid, {"overall_sentiment": 0.0, "assets": []})
            assets = res.get("assets", [])

            if not assets:
                cur.execute("""
                    UPDATE yggdrasil.mimir_raw_articles 
                    SET scoring_status = 'empty' 
                    WHERE id = %s
                """, (aid,))
                continue

            impacts = []
            for asset in assets:
                asset_name = asset.get("asset_name", "").strip()
                if not asset_name:
                    continue

                ticker, _ = resolve_ticker(asset_name)
                country = asset.get("country")
                if country:
                    country = resolve_country_code(country) or country
                else:
                    country = resolve_country_code(asset_name)
                resolved_reg = resolve_region(country) if country else None
                region = resolved_reg or asset.get("region")

                impacts.append((
                    aid,
                    asset_name,
                    asset.get("asset_category", "UNKNOWN"),
                    asset.get("sub_category"),
                    country,
                    region,
                    asset.get("sentiment_score", 0.0),
                    asset.get("confidence", 0.5),
                    asset.get("direction", "neutral"),
                    asset.get("magnitude", "MEDIUM"),
                    asset.get("reasoning", ""),
                    ticker,
                    asset.get("policy_signal"),
                    False,  # is_spillover
                    None,   # spillover_source_article_id
                    None,   # spillover_source_asset
                    now_ts, # activation_date
                ))

            if not impacts:
                cur.execute("""
                    UPDATE yggdrasil.mimir_raw_articles 
                    SET scoring_status = 'empty' 
                    WHERE id = %s
                """, (aid,))
                continue

            sql = """
            INSERT INTO yggdrasil.mimir_sentiment_impacts (
                article_id, asset_name, asset_category, asset_sub_category,
                country, region, sentiment_score, confidence, direction,
                magnitude, reasoning, ticker, policy_signal,
                is_spillover, spillover_source_article_id, spillover_source_asset, activation_date
            ) VALUES %s
            ON CONFLICT (article_id, asset_name) DO NOTHING;
            """
            execute_values(cur, sql, impacts)
            inserted = cur.rowcount
            total_impacts_inserted += inserted

            # Trigger LLM Supply Chain Discovery for high-impact headlines (|score| >= 0.75)
            for imp in impacts:
                score = imp[6]
                ticker_val = imp[11]
                if ticker_val and abs(score) >= 0.75:
                    try:
                        from backend.app.sentiment.supply_chain_mapper import discover_llm_supply_chain
                        discover_llm_supply_chain(title, summary or "", ticker_val, score, conn=conn)
                    except Exception as llm_err:
                        print(f"  [Thread] [Warning] LLM supply chain discovery error for {ticker_val}: {llm_err}")

            # Compute spillover impacts
            try:
                asset_dicts = [{
                    "asset_name": imp[1],
                    "asset_category": imp[2],
                    "sub_category": imp[3],
                    "country": imp[4],
                    "region": imp[5],
                    "sentiment_score": imp[6],
                    "confidence": imp[7],
                    "ticker": imp[11],
                } for imp in impacts]

                engine = _get_spillover_engine()
                graph_spills = engine.run(aid, asset_dicts, published_ts=now_ts)

                detector = _get_thematic_detector()
                thematic_spills = detector.compute_spillovers(aid, asset_dicts, title, summary or "")

                all_spills = graph_spills + thematic_spills
                if all_spills:
                    execute_values(cur, sql, all_spills)
                    total_impacts_inserted += cur.rowcount
            except Exception as spill_err:
                print(f"  [Thread] [Warning] Spillover skipped for article {aid}: {spill_err}")

            cur.execute("""
                UPDATE yggdrasil.mimir_raw_articles
                SET scoring_status = 'scored'
                WHERE id = %s
            """, (aid,))

        conn.commit()
        return total_impacts_inserted

    except Exception as e:
        print(f"  [Batch] Error processing batch of articles: {e}")
        if conn:
            conn.rollback()
            for aid, _, _ in batch_articles:
                try:
                    cur.execute("""
                        UPDATE yggdrasil.mimir_raw_articles 
                        SET scoring_status = 'pending' 
                        WHERE id = %s
                    """, (aid,))
                except Exception:
                    pass
            conn.commit()
        return -1
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def process_unscored_articles(batch_size: int = 50, max_workers: int = 4) -> int:
    """
    Fetch a batch of unscored articles and process them in 5-article LLM batches in parallel.
    Returns total number of inserted asset impacts.
    """
    # --- Get pending articles ---
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, title, summary
        FROM yggdrasil.mimir_raw_articles
        WHERE scoring_status = 'pending'
        ORDER BY published_ts DESC NULLS LAST
        LIMIT %s
    """, (batch_size,))
    articles = cur.fetchall()
    cur.close()
    conn.close()

    if not articles:
        print("[MIMIR] No pending articles found.")
        return 0

    # Group articles into chunks of 5
    CHUNK_SIZE = 5
    chunks = [articles[i:i + CHUNK_SIZE] for i in range(0, len(articles), CHUNK_SIZE)]

    print(f"[MIMIR] Processing {len(articles)} articles in {len(chunks)} batched LLM calls across {max_workers} parallel workers...")

    total_inserted = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_article_batch, chunk) for chunk in chunks]
        for future in as_completed(futures):
            try:
                res = future.result()
                if res > 0:
                    total_inserted += res
            except Exception as e:
                print(f"  [Main] Batch processing error: {e}")

    print(f"[MIMIR] Total impacts inserted: {total_inserted}")
    
    # Run asset category clean up queries
    if total_inserted > 0:
        try:
            conn_cleanup = get_db_connection()
            cur_cleanup = conn_cleanup.cursor()
            print("[MIMIR] Executing asset category replacements (SECTOR -> EQUITY)...")
            cur_cleanup.execute("""
                UPDATE yggdrasil.mimir_sentiment_impacts
                SET asset_category = REPLACE(asset_category, 'SECTOR', 'EQUITY')
                WHERE asset_category LIKE '%SECTOR%';
                
                -- Standardize equity sub_categories to 11 standard GICS sectors
                UPDATE yggdrasil.mimir_sentiment_impacts
                SET asset_sub_category = 
                    CASE 
                        WHEN asset_sub_category IN ('FINANCIALS', 'FINANCIAL') THEN 'FINANCIAL_SERVICES'
                        WHEN asset_sub_category IN ('COMMUNICATION', 'COMMUNICATIONS', 'MEDIA') THEN 'COMMUNICATION_SERVICES'
                        WHEN asset_sub_category IN ('MATERIALS', 'PRECIOUS_METALS', 'BASE_METALS') THEN 'BASIC_MATERIALS'
                        WHEN asset_sub_category IN ('CONSUMER_DISCRETIONARY', 'CONSUMER DISCRETIONARY') THEN 'CONSUMER_CYCLICAL'
                        WHEN asset_sub_category IN ('AIRLINE', 'AIRLINES', 'AIRPORTS', 'TRANSPORTATION') THEN 'INDUSTRIALS'
                        WHEN asset_sub_category IS NULL OR asset_sub_category = 'CORPORATE' THEN 'TECHNOLOGY'
                        ELSE UPPER(TRIM(asset_sub_category))
                    END
                WHERE asset_category = 'EQUITY';
            """)
            conn_cleanup.commit()
            cur_cleanup.close()
            conn_cleanup.close()
            print("[MIMIR] Asset category replacements and sector standardizations completed.")
        except Exception as e:
            print(f"[MIMIR] Error running asset category replacements: {e}")

    return total_inserted


def reset_article_status(article_id: int):
    """Reset an article's status to 'pending' for re-processing."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE yggdrasil.mimir_raw_articles 
        SET scoring_status = 'pending' 
        WHERE id = %s
    """, (article_id,))
    conn.commit()
    cur.close()
    conn.close()
    print(f"[MIMIR] Article {article_id} reset to 'pending'")


def get_status_counts() -> dict:
    """Get counts of articles by scoring status."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT scoring_status, COUNT(*) 
        FROM yggdrasil.mimir_raw_articles 
        GROUP BY scoring_status
    """)
    results = {row[0]: row[1] for row in cur.fetchall()}
    cur.close()
    conn.close()
    return results

def run_triage_batch(articles: List[tuple]) -> List[dict]:
    """DEPRECATED: Use triage_pending_articles() local Python triage instead."""
    return []

def triage_pending_articles(batch_size: int = 200) -> int:
    """
    Fetches articles in 'triage_pending' status, triages them locally using
    fast Python regex (DeepSeekSentiment.is_financial_or_macro), and updates
    their status to 'pending' (if relevant) or 'ignored' (if not).
    ZERO LLM API COST.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Fetch triage_pending articles
    cur.execute("""
        SELECT id, title, summary 
        FROM yggdrasil.mimir_raw_articles 
        WHERE scoring_status = 'triage_pending'
        ORDER BY published_ts DESC NULLS LAST
        LIMIT %s
    """, (batch_size,))
    articles = cur.fetchall()
    
    if not articles:
        cur.close()
        conn.close()
        return 0
        
    print(f"[TRIAGE] Triaging {len(articles)} articles locally via Python regex (0 LLM tokens)...")
    
    client = DeepSeekSentiment()
    relevant_ids = []
    ignored_ids = []
    
    for aid, title, summary in articles:
        if client.is_financial_or_macro(title, summary or ""):
            relevant_ids.append(aid)
        else:
            ignored_ids.append(aid)
            
    if relevant_ids:
        cur.execute("""
            UPDATE yggdrasil.mimir_raw_articles 
            SET scoring_status = 'pending' 
            WHERE id = ANY(%s)
        """, (relevant_ids,))
        print(f" [TRIAGE] {len(relevant_ids)} articles marked as PENDING (relevant)")
        
    if ignored_ids:
        cur.execute("""
            UPDATE yggdrasil.mimir_raw_articles 
            SET scoring_status = 'ignored' 
            WHERE id = ANY(%s)
        """, (ignored_ids,))
        print(f" [TRIAGE] {len(ignored_ids)} articles marked as IGNORED (not relevant)")
        
    conn.commit()
    cur.close()
    conn.close()
    return len(articles)