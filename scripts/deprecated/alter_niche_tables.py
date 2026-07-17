import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from backend.app.database import get_db_connection

ALTER_TABLE_SQL = """
ALTER TABLE yggdrasil.mimir_pair_signals
ADD COLUMN IF NOT EXISTS sentiment_filter NUMERIC,
ADD COLUMN IF NOT EXISTS conviction_score VARCHAR(50);
"""

def main():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        print("Altering table yggdrasil.mimir_pair_signals...")
        cur.execute(ALTER_TABLE_SQL)
        conn.commit()
        print("[OK] Table altered successfully.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ERROR] Error altering tables: {e}")

if __name__ == "__main__":
    main()
