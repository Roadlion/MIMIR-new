# scripts/alter_articles_scoring_status_default.py
import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()

def main():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        print("[MIGRATION] Altering scoring_status default value to 'triage_pending'...")
        cur.execute("ALTER TABLE yggdrasil.mimir_raw_articles ALTER COLUMN scoring_status SET DEFAULT 'triage_pending';")
        # Also update any existing articles that are still 'pending' to 'triage_pending' so they go through triage
        cur.execute("UPDATE yggdrasil.mimir_raw_articles SET scoring_status = 'triage_pending' WHERE scoring_status = 'pending';")
        conn.commit()
        print("[MIGRATION] Table default alter completed successfully.")
    except Exception as e:
        conn.rollback()
        print(f"[MIGRATION] ERROR: {e}")
        sys.exit(1)
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    main()
