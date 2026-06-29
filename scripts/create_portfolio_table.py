# scripts/create_portfolio_table.py
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {settings.mimir_schema}.mimir_portfolio (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(20) NOT NULL,
    order_date TIMESTAMPTZ NOT NULL,
    buy_price NUMERIC NOT NULL,
    quantity NUMERIC NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mimir_portfolio_ticker ON {settings.mimir_schema}.mimir_portfolio (ticker);
"""

def main():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        print(f"Creating table {settings.mimir_schema}.mimir_portfolio...")
        cur.execute(CREATE_TABLE_SQL)
        conn.commit()
        print("[OK] Portfolio table created successfully.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ERROR] Error creating table: {e}")

if __name__ == "__main__":
    main()
