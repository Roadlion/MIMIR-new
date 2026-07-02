# scripts/migrate_portfolio_sales.py
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()

MIGRATION_SQL = f"""
-- Add transaction_type column to mimir_portfolio
ALTER TABLE {settings.mimir_schema}.mimir_portfolio 
ADD COLUMN IF NOT EXISTS transaction_type VARCHAR(10) NOT NULL DEFAULT 'BUY';

-- Add check constraint to ensure only BUY or SELL is allowed
ALTER TABLE {settings.mimir_schema}.mimir_portfolio 
DROP CONSTRAINT IF EXISTS chk_transaction_type;

ALTER TABLE {settings.mimir_schema}.mimir_portfolio 
ADD CONSTRAINT chk_transaction_type CHECK (transaction_type IN ('BUY', 'SELL'));
"""

def main():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        print(f"Applying portfolio migration in schema {settings.mimir_schema}...")
        cur.execute(MIGRATION_SQL)
        conn.commit()
        print("[OK] Portfolio migration completed successfully.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ERROR] Error executing migration: {e}")

if __name__ == "__main__":
    main()
