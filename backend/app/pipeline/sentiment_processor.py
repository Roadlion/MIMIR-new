# backend/app/pipeline/sentiment_processor.py
import psycopg2
from psycopg2.extras import execute_values
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional
from backend.app.database import get_db_connection
from backend.app.sentiment.deepseek_client import DeepSeekSentiment
from backend.app.sentiment.asset_mapper import resolve_ticker, resolve_country_code, resolve_region


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
            print(f"  [Thread] Article {article_id}: No assets → marked empty")
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
            region = asset.get("region")
            if not region and country:
                region = resolve_region(country)

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
                asset.get("policy_signal")
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

        # --- Insert impacts ---
        sql = """
        INSERT INTO yggdrasil.mimir_sentiment_impacts (
            article_id, asset_name, asset_category, asset_sub_category,
            country, region, sentiment_score, confidence, direction,
            magnitude, reasoning, ticker, policy_signal
        ) VALUES %s
        ON CONFLICT (article_id, asset_name) DO NOTHING;
        """
        execute_values(cur, sql, impacts)
        conn.commit()
        inserted = cur.rowcount

        # --- Mark article as 'scored' ---
        cur.execute("""
            UPDATE yggdrasil.mimir_raw_articles 
            SET scoring_status = 'scored' 
            WHERE id = %s
        """, (article_id,))
        conn.commit()

        print(f"  [Thread] Article {article_id}: Inserted {inserted} impacts → marked scored")
        return inserted

    except Exception as e:
        print(f"  [Thread] ❌ Error on article {article_id}: {e}")
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


def process_unscored_articles(batch_size: int = 50, max_workers: int = 5) -> int:
    """
    Fetch a batch of unscored articles and process them in parallel using ThreadPoolExecutor.
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

    print(f"[MIMIR] Processing {len(articles)} articles with {max_workers} parallel workers...")

    total_inserted = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        future_to_article = {
            executor.submit(process_single_article, article_id, title, summary): article_id
            for article_id, title, summary in articles
        }

        # Process results as they complete
        for future in as_completed(future_to_article):
            article_id = future_to_article[future]
            try:
                result = future.result()
                if result > 0:
                    total_inserted += result
                elif result == 0:
                    # empty – already logged in thread
                    pass
                else:  # -1 error – already marked pending
                    pass
            except Exception as e:
                print(f"  [Main] Unexpected error for article {article_id}: {e}")
                # Optionally mark as pending again, but the thread should have handled it
                # We'll do a fallback update
                try:
                    conn2 = get_db_connection()
                    cur2 = conn2.cursor()
                    cur2.execute("""
                        UPDATE yggdrasil.mimir_raw_articles 
                        SET scoring_status = 'pending' 
                        WHERE id = %s AND scoring_status = 'scored'
                    """, (article_id,))
                    conn2.commit()
                    cur2.close()
                    conn2.close()
                except Exception:
                    pass

    print(f"[MIMIR] Total impacts inserted: {total_inserted}")
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