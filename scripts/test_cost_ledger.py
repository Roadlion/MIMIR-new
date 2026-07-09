# scripts/test_cost_ledger.py
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
        cur.execute(f"SELECT * FROM {settings.mimir_schema}.mimir_api_cost_ledger ORDER BY created_at DESC;")
        rows = cur.fetchall()
        print("="*60)
        print(f"API COST LEDGER RECORDS ({len(rows)} entries):")
        print("="*60)
        for r in rows:
            print(f"ID: {r[0]} | Service: {r[1]} | Prompt: {r[2]} | Comp: {r[3]} | Cost: ${r[4]:.6f} | Items: {r[5]} | Created: {r[6]}")
        
        cur.execute(f"SELECT SUM(cost_usd) FROM {settings.mimir_schema}.mimir_api_cost_ledger;")
        sum_cost = cur.fetchone()[0]
        print("="*60)
        print(f"GRAND TOTAL API COST: ${sum_cost if sum_cost is not None else 0.0:.6f}")
        print("="*60)
    except Exception as e:
        print(f"ERROR: {e}")
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    main()
