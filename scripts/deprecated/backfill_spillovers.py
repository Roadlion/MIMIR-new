#!/usr/bin/env python3
"""
One-shot backfill: compute spillover impacts for all existing scored articles.
Run after the migration + seed to enrich historical data.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.database import get_db_connection
from backend.app.pipeline.spillover_engine import SpilloverEngine
from backend.app.sentiment.thematic_detector import ThematicDetector
from backend.app.sentiment.relationship_graph import refresh_relationship_graph
from psycopg2.extras import execute_values

INSERT_SQL = """
INSERT INTO yggdrasil.mimir_sentiment_impacts (
    article_id, asset_name, asset_category, asset_sub_category,
    country, region, sentiment_score, confidence, direction,
    magnitude, reasoning, ticker, policy_signal,
    is_spillover, spillover_source_article_id, spillover_source_asset
) VALUES %s
ON CONFLICT (article_id, asset_name) DO NOTHING;
"""

BATCH_SIZE = 100


def main():
    print("[BACKFILL] Loading relationship graph...")
    n = refresh_relationship_graph()
    print(f"[BACKFILL] Graph loaded: {n} edges")

    engine = SpilloverEngine()
    detector = ThematicDetector()

    conn = get_db_connection()
    cur = conn.cursor()

    # Find articles with direct impacts but no spillover impacts yet
    cur.execute("""
        SELECT a.id, a.title, a.summary
        FROM yggdrasil.mimir_raw_articles a
        WHERE a.scoring_status = 'scored'
          AND EXISTS (
              SELECT 1 FROM yggdrasil.mimir_sentiment_impacts si
              WHERE si.article_id = a.id AND si.is_spillover = FALSE
          )
          AND NOT EXISTS (
              SELECT 1 FROM yggdrasil.mimir_sentiment_impacts si
              WHERE si.article_id = a.id AND si.is_spillover = TRUE
          )
        ORDER BY a.published_ts DESC
        LIMIT 5000
    """)
    articles = cur.fetchall()
    print(f"[BACKFILL] Found {len(articles)} articles needing spillover backfill")

    if not articles:
        cur.close()
        conn.close()
        print("[BACKFILL] Nothing to do.")
        return

    total_direct = 0
    total_spill = 0
    processed = 0

    for article_id, title, summary in articles:
        # Fetch direct impacts for this article
        cur.execute("""
            SELECT asset_name, asset_category, asset_sub_category,
                   country, region, sentiment_score, confidence,
                   ticker
            FROM yggdrasil.mimir_sentiment_impacts
            WHERE article_id = %s AND is_spillover = FALSE
        """, (article_id,))
        direct_rows = cur.fetchall()

        if not direct_rows:
            continue

        asset_dicts = []
        for row in direct_rows:
            asset_dicts.append({
                "asset_name": row[0],
                "asset_category": row[1],
                "sub_category": row[2],
                "country": row[3],
                "region": row[4],
                "sentiment_score": float(row[5] or 0),
                "confidence": float(row[6] or 0.5),
                "ticker": row[7],
            })

        total_direct += len(asset_dicts)

        # Compute spillovers
        graph_spills = engine.run(article_id, asset_dicts)
        thematic_spills = detector.compute_spillovers(
            article_id, asset_dicts, title or "", summary or "",
        )

        all_spills = graph_spills + thematic_spills
        if all_spills:
            try:
                execute_values(cur, INSERT_SQL, all_spills)
                conn.commit()
                total_spill += cur.rowcount
            except Exception as e:
                conn.rollback()
                print(f"  [WARN] Article {article_id}: {e}")

        processed += 1
        if processed % 100 == 0:
            print(f"  [BACKFILL] {processed}/{len(articles)} articles, "
                  f"{total_spill} spillovers so far...")

    cur.close()
    conn.close()

    print(f"\n[BACKFILL] Done. {processed} articles processed.")
    print(f"  Direct impacts: {total_direct}")
    print(f"  Spillover impacts created: {total_spill}")


if __name__ == "__main__":
    main()
