# scripts/fix_portfolio_leak.py
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.database import get_db_connection, get_db_connection_dict
from backend.app.config import get_settings

settings = get_settings()

def fix_portfolio():
    conn = get_db_connection_dict()
    cur = conn.cursor()
    try:
        schema = settings.mimir_schema
        # Check current count of paper trades in mimir_portfolio
        cur.execute(f"SELECT COUNT(*) as count FROM {schema}.mimir_portfolio WHERE source = 'PAPER_ALERT'")
        count_before = cur.fetchone()["count"]
        print(f"[FIX] Found {count_before} leaked paper trade records in mimir_portfolio.")

        if count_before > 0:
            cur.execute(f"DELETE FROM {schema}.mimir_portfolio WHERE source = 'PAPER_ALERT'")
            conn.commit()
            print(f"[FIX] Successfully removed {count_before} paper trade records from mimir_portfolio.")

        # Check remaining manual records
        cur.execute(f"SELECT COUNT(*) as count FROM {schema}.mimir_portfolio WHERE source IS NULL OR source = 'MANUAL'")
        manual_count = cur.fetchone()["count"]
        print(f"[FIX] Real portfolio now has {manual_count} transactions.")

    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    fix_portfolio()
