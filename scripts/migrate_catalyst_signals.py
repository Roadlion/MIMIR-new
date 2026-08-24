# scripts/migrate_catalyst_signals.py
import sys
import os

# Add root directory to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.app.database import get_db_connection
from backend.app.config import get_settings

def migrate_schema():
    settings = get_settings()
    conn = get_db_connection()
    cur = conn.cursor()
    schema = settings.mimir_schema

    print(f"[MIGRATION] Checking & updating {schema}.mimir_trade_signals table schema...")

    columns_to_add = [
        ("catalyst_type", "VARCHAR(50)"),
        ("holding_period", "VARCHAR(100)"),
        ("investment_thesis", "TEXT"),
        ("headline", "TEXT"),
        ("target_price", "DOUBLE PRECISION"),
        ("stop_loss", "DOUBLE PRECISION"),
        ("conviction_score", "DOUBLE PRECISION"),
    ]

    try:
        for col_name, col_type in columns_to_add:
            sql = f"""
                ALTER TABLE {schema}.mimir_trade_signals
                ADD COLUMN IF NOT EXISTS {col_name} {col_type};
            """
            cur.execute(sql)
            print(f"[MIGRATION] Column '{col_name}' ({col_type}) ensured.")

        conn.commit()
        print("[MIGRATION] Schema migration completed successfully.")
    except Exception as e:
        conn.rollback()
        print(f"[MIGRATION ERROR] {e}")
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    migrate_schema()
