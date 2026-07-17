# scripts/migrate_portfolio_fees.py
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()

MIGRATION_SQL = f"""
-- Add fee columns to mimir_portfolio
ALTER TABLE {settings.mimir_schema}.mimir_portfolio 
ADD COLUMN IF NOT EXISTS brokerage_fee NUMERIC NOT NULL DEFAULT 0.0,
ADD COLUMN IF NOT EXISTS regulatory_fee NUMERIC NOT NULL DEFAULT 0.0,
ADD COLUMN IF NOT EXISTS other_fee NUMERIC NOT NULL DEFAULT 0.0;
"""

def main():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        print(f"Applying portfolio fees migration in schema {settings.mimir_schema}...")
        cur.execute(MIGRATION_SQL)
        conn.commit()
        print("[OK] Portfolio fees migration completed successfully.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ERROR] Error executing migration: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
