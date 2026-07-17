# scripts/create_prices_table.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from backend.app.database import get_db_connection

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS yggdrasil.mimir_hourly_prices (
    ticker VARCHAR(50) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    price NUMERIC NOT NULL,
    volume BIGINT,
    scraped_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (ticker, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_mimir_hourly_prices_timestamp ON yggdrasil.mimir_hourly_prices (timestamp DESC);
"""

def main():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        print("Creating table yggdrasil.mimir_hourly_prices...")
        cur.execute(CREATE_TABLE_SQL)
        conn.commit()
        print("[OK] Table created successfully.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ERROR] Error creating table: {e}")

if __name__ == "__main__":
    main()
