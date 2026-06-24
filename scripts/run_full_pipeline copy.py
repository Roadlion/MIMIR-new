# scripts/run_full_pipeline.py
# Process ALL unscored articles in batches, with failsafes.
# Runs until all pending articles are processed.

import sys
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.pipeline.sentiment_processor import process_unscored_articles, get_status_counts
from backend.app.database import get_db_connection


def reset_failed_articles():
    """Reset failed articles to pending for retry."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE yggdrasil.mimir_raw_articles 
        SET scoring_status = 'pending' 
        WHERE scoring_status = 'failed'
    """)
    conn.commit()
    reset_count = cur.rowcount
    cur.close()
    conn.close()
    if reset_count > 0:
        print(f"   🔄 Reset {reset_count} failed articles to pending for retry.")
    return reset_count


def main():
    BATCH_SIZE = 50
    SLEEP_BETWEEN_BATCHES = 2
    MAX_RETRIES = 3
    retry_count = 0

    print("=" * 60)
    print("🚀 MIMIR FULL SENTIMENT PIPELINE")
    print("   Processing ALL pending articles until complete.")
    print("=" * 60)

    # Show current status
    status = get_status_counts()
    print("📊 Current status:")
    print(f"   Pending:  {status.get('pending', 0)} (to be scored)")
    print(f"   Scored:   {status.get('scored', 0)} (already done)")
    print(f"   Empty:    {status.get('empty', 0)} (no assets found, skipped)")
    print(f"   Failed:   {status.get('failed', 0)} (API errors, will retry)")
    print("=" * 60)

    total_pending = status.get('pending', 0)
    if total_pending == 0:
        # Check if there are failed articles to retry
        if status.get('failed', 0) > 0:
            print(f"⚠️ Found {status.get('failed', 0)} failed articles. Resetting...")
            reset_failed_articles()
            status = get_status_counts()
            total_pending = status.get('pending', 0)
            if total_pending == 0:
                print("✅ No pending articles after reset. All done.")
                return
        else:
            print("✅ No pending articles. All done.")
            return

    print(f"📦 Batch size: {BATCH_SIZE}")
    print(f"📊 Total pending: {total_pending}")
    print("=" * 60)

    processed_total = 0
    batch_num = 0
    consecutive_empty_batches = 0
    max_empty_batches = 3

    while True:
        # Check if still pending
        status = get_status_counts()
        pending = status.get('pending', 0)
        failed = status.get('failed', 0)

        if pending == 0 and failed == 0:
            print("\n✅ All articles processed successfully.")
            break

        # If only failed remain, reset them and continue
        if pending == 0 and failed > 0:
            print(f"\n⚠️ {failed} failed articles remaining. Resetting...")
            reset_failed_articles()
            time.sleep(2)
            continue

        batch_num += 1
        print(f"\n📦 Batch {batch_num} — {pending} articles remaining")

        try:
            inserted = process_unscored_articles(BATCH_SIZE)
            processed_total += inserted
            print(f"   ✅ Inserted {inserted} asset impacts this batch.")
            
            if inserted == 0:
                consecutive_empty_batches += 1
                print(f"   ⚠️ No impacts inserted (consecutive: {consecutive_empty_batches}/{max_empty_batches})")
                
                # Check if there are pending articles stuck
                status = get_status_counts()
                if status.get('pending', 0) > 0:
                    # Try resetting failed articles if any
                    if status.get('failed', 0) > 0:
                        print("   🔄 Resetting failed articles...")
                        reset_failed_articles()
                        continue
                    
                    if consecutive_empty_batches >= max_empty_batches:
                        print(f"   ❌ {max_empty_batches} empty batches in a row. Possible stuck articles.")
                        print("   🔍 Manual intervention needed. Check for articles with status='failed'.")
                        break
                else:
                    # No pending left, break out
                    print("   ℹ️ No pending articles remaining.")
                    break
            else:
                consecutive_empty_batches = 0

        except Exception as e:
            print(f"   ❌ Batch failed: {e}")
            retry_count += 1
            if retry_count >= MAX_RETRIES:
                print(f"   ❌ Max retries ({MAX_RETRIES}) reached. Stopping.")
                break
            print(f"   ⏳ Waiting 10s before retry... (attempt {retry_count}/{MAX_RETRIES})")
            time.sleep(10)
            continue

        time.sleep(SLEEP_BETWEEN_BATCHES)

    # Final stats
    status = get_status_counts()
    print("\n" + "=" * 60)
    print("📊 FINAL SUMMARY")
    print("=" * 60)
    print(f"Total batches run:    {batch_num}")
    print(f"Total asset impacts inserted: {processed_total}")
    print(f"Status counts:")
    print(f"   Pending:  {status.get('pending', 0)}")
    print(f"   Scored:   {status.get('scored', 0)}")
    print(f"   Empty:    {status.get('empty', 0)}")
    print(f"   Failed:   {status.get('failed', 0)}")
    print("=" * 60)

    # Refresh materialized view if all done
    if status.get('pending', 0) == 0 and status.get('failed', 0) == 0:
        print("🔄 Refreshing materialized view...")
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT refresh_mimir_aggregates();")
            conn.commit()
            cur.close()
            conn.close()
            print("✅ Materialized view refreshed.")
        except Exception as e:
            print(f"⚠️ Failed to refresh view: {e}")
    else:
        print("⚠️ Some articles remain unprocessed. Run the script again to continue.")


if __name__ == "__main__":
    main()