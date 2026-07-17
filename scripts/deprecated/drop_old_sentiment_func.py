# scripts/drop_old_sentiment_func.py
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()

def main():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        print("Dropping old 5-parameter mimir_weighted_sentiment function to avoid overload ambiguity...")
        
        # SQL to drop the old overload
        sql = f"""
        DROP FUNCTION IF EXISTS {settings.mimir_schema}.mimir_weighted_sentiment(TEXT, TEXT, INTEGER, NUMERIC, BOOLEAN);
        """
        cur.execute(sql)
        conn.commit()
        print("[OK] Outdated function dropped successfully.")
        
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ERROR] Error executing drop: {e}")

if __name__ == "__main__":
    main()
