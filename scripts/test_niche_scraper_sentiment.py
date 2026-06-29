"""Test the Guerilla Quant niche scraper and sentiment pipeline directly."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.pipeline.background_worker import run_niche_scan
from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()

def main():
    print("="*60)
    print("RUNNING DIRECT NICHE SCRAPER + SENTIMENT PIPELINE")
    print("="*60)

    # 1. Clear old niche articles/signals if any, or just run to check insertions
    conn = get_db_connection()
    cur = conn.cursor()
    
    # Clean slate for testing
    cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_raw_articles WHERE source_name LIKE 'niche-%%'")
    conn.commit()
    
    # Get current counts
    cur.execute(f"SELECT COUNT(*) FROM {settings.mimir_schema}.mimir_raw_articles WHERE source_name LIKE 'niche-%%'")
    before_art = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM {settings.mimir_schema}.mimir_sentiment_impacts")
    before_imp = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM {settings.mimir_schema}.mimir_pair_signals")
    before_sig = cur.fetchone()[0]
    
    print(f"Database Stats BEFORE Scan:")
    print(f"   Niche Articles: {before_art}")
    print(f"   Sentiment Impacts: {before_imp}")
    print(f"   Pair Signals: {before_sig}")
    print("-"*60)
    
    # 2. Run the niche scan
    print("Executing background_worker.run_niche_scan()...")
    try:
        run_niche_scan()
        print("run_niche_scan() completed.")
    except Exception as e:
        print(f"run_niche_scan() failed: {e}")
    
    print("-"*60)
    
    # 3. Get counts after scan
    cur.execute(f"SELECT COUNT(*) FROM {settings.mimir_schema}.mimir_raw_articles WHERE source_name LIKE 'niche-%%'")
    after_art = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM {settings.mimir_schema}.mimir_sentiment_impacts")
    after_imp = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM {settings.mimir_schema}.mimir_pair_signals")
    after_sig = cur.fetchone()[0]
    
    print(f"Database Stats AFTER Scan:")
    print(f"   Niche Articles: {after_art} (Diff: +{after_art - before_art})")
    print(f"   Sentiment Impacts: {after_imp} (Diff: +{after_imp - before_imp})")
    print(f"   Pair Signals: {after_sig} (Diff: +{after_sig - before_sig})")
    
    # 4. Show latest 5 niche sentiment impacts
    cur.execute(f"""
        SELECT si.ticker, si.sentiment_score, si.direction, a.title
        FROM {settings.mimir_schema}.mimir_sentiment_impacts si
        JOIN {settings.mimir_schema}.mimir_raw_articles a ON a.id = si.article_id
        WHERE a.source_name LIKE 'niche-%%'
        ORDER BY a.id DESC LIMIT 5
    """)
    rows = cur.fetchall()
    if rows:
        print("\nLatest Niche Sentiment Impacts:")
        for r in rows:
            print(f"   [{r[0]}] Score: {r[1]:.2f} ({r[2]}) -> {r[3][:60]}...")
    else:
        print("\nNo niche sentiment impacts found. Check if DeepSeek API key is correct and responding.")

    cur.close()
    conn.close()
    print("="*60)

if __name__ == "__main__":
    main()
