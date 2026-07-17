"""Add mean_spread, current_spread, conviction columns to mimir_pair_signals."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from backend.app.database import get_db_connection

SQL = """
ALTER TABLE yggdrasil.mimir_pair_signals
  ADD COLUMN IF NOT EXISTS mean_spread NUMERIC,
  ADD COLUMN IF NOT EXISTS current_spread NUMERIC,
  ADD COLUMN IF NOT EXISTS conviction VARCHAR(100);
"""

def main():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(SQL)
        conn.commit()
        print("[OK] Columns added to yggdrasil.mimir_pair_signals.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
