# scripts/run_full_pipeline.py
# Process ALL unscored articles in batches, with failsafes.

import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.pipeline.sentiment_processor import process_unscored_articles, get_status_counts, triage_pending_articles
from backend.app.database import get_db_connection

def main():
    BATCH_SIZE = 50
    MAX_BATCHES = 1
    SLEEP_BETWEEN_BATCHES = 2

    print("="*60)
    print("[PIPELINE] MIMIR FULL SENTIMENT PIPELINE")
    print("="*60)

    # 1. Run batch triage pre-filtering on newly scraped articles
    print("[PIPELINE] Running LLM Triage Gatekeeper pre-filtering...")
    try:
        triaged_count = triage_pending_articles(100)
        print(f"[PIPELINE] Successfully triaged {triaged_count} articles.")
    except Exception as e:
        print(f"[PIPELINE] Triage pre-filtering failed: {e}")
    print("="*60)

    # Show current status
    status = get_status_counts()
    print("[STATUS] Current status:")
    print(f"   Triage Pending: {status.get('triage_pending', 0)} (awaiting triage)")
    print(f"   Pending:        {status.get('pending', 0)} (to be scored)")
    print(f"   Scored:         {status.get('scored', 0)} (already done)")
    print(f"   Ignored:        {status.get('ignored', 0)} (filtered out)")
    print(f"   Empty:          {status.get('empty', 0)} (no assets found, skipped)")
    print(f"   Failed:         {status.get('failed', 0)} (API errors, will retry)")
    print("="*60)

    total_pending = status.get('pending', 0)
    if total_pending == 0:
        print("[ok] No pending articles. All done.")
        return

    print(f"[INFO] Batch size: {BATCH_SIZE}")
    if MAX_BATCHES:
        print(f"[INFO] Max batches: {MAX_BATCHES}")
    print("="*60)

    processed_total = 0
    batch_num = 0

    while True:
        # Check if still pending
        status = get_status_counts()
        pending = status.get('pending', 0)
        if pending == 0:
            print("\n[ok] All articles processed.")
            break

        if MAX_BATCHES and batch_num >= MAX_BATCHES:
            print(f"\n[stop] Reached max batches ({MAX_BATCHES}). Stopping.")
            break

        batch_num += 1
        print(f"\n[BATCH] Batch {batch_num} -- {pending} articles remaining")

        try:
            inserted = process_unscored_articles(BATCH_SIZE)
            processed_total += inserted
            print(f"   [ok] Inserted {inserted} asset impacts this batch.")
        except Exception as e:
            print(f"   [error] Batch failed: {e}")
            print("   [wait] Waiting 10s before retry...")
            time.sleep(10)
            continue

        if inserted == 0:
            # Check if there are pending articles stuck
            status = get_status_counts()
            if status.get('pending', 0) > 0:
                print("   [!] No new impacts, but pending articles remain.")
                print("   [!] Check for articles with status='failed' that need retry.")
            break

        time.sleep(SLEEP_BETWEEN_BATCHES)

    # Final stats
    status = get_status_counts()
    print("\n" + "="*60)
    print("[FINAL] FINAL SUMMARY")
    print("="*60)
    print(f"Total batches run:    {batch_num}")
    print(f"Total asset impacts inserted: {processed_total}")
    print(f"Status counts:")
    print(f"   Pending:  {status.get('pending', 0)}")
    print(f"   Scored:   {status.get('scored', 0)}")
    print(f"   Empty:    {status.get('empty', 0)}")
    print(f"   Failed:   {status.get('failed', 0)}")
    print("="*60)

    if status.get('pending', 0) == 0:
        print("[refresh] Refreshing materialized view...")
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT refresh_mimir_aggregates();")
            conn.commit()
            cur.close()
            conn.close()
            print("[ok] Materialized view refreshed.")
        except Exception as e:
            print(f"[error] Failed to refresh view: {e}")

if __name__ == "__main__":
    main()