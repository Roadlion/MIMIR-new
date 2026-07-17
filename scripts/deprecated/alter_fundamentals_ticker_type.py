# scripts/alter_fundamentals_ticker_type.py
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
        print("[MIGRATION] Altering ticker column to VARCHAR(50)...")
        cur.execute(f"ALTER TABLE {settings.mimir_schema}.mimir_asset_fundamentals ALTER COLUMN ticker TYPE VARCHAR(50);")
        conn.commit()
        print("[MIGRATION] Alter table completed successfully.")
    except Exception as e:
        conn.rollback()
        print(f"[MIGRATION] ERROR: {e}")
        sys.exit(1)
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    main()
