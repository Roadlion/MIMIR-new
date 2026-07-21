import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()

MIGRATION_SQL = f"""
ALTER TABLE {settings.mimir_schema}.mimir_portfolio 
DROP CONSTRAINT IF EXISTS chk_transaction_type;

ALTER TABLE {settings.mimir_schema}.mimir_portfolio 
ADD CONSTRAINT chk_transaction_type CHECK (transaction_type IN ('BUY', 'SELL', 'DIVIDEND'));
"""

def main():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        print(f"Altering table {settings.mimir_schema}.mimir_portfolio check constraints to allow 'DIVIDEND'...")
        cur.execute(MIGRATION_SQL)
        conn.commit()
        print("[OK] Check constraint modified successfully.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ERROR] Error applying migration: {e}")

if __name__ == "__main__":
    main()
