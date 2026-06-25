# scripts/create_niche_tables.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from backend.app.database import get_db_connection

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS yggdrasil.mimir_niche_assets (
    ticker VARCHAR(50) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    asset_class VARCHAR(50) NOT NULL,
    exchange VARCHAR(50),
    tick_size NUMERIC
);

CREATE TABLE IF NOT EXISTS yggdrasil.mimir_niche_prices (
    ticker VARCHAR(50) NOT NULL REFERENCES yggdrasil.mimir_niche_assets(ticker),
    timestamp TIMESTAMPTZ NOT NULL,
    price NUMERIC NOT NULL,
    volume BIGINT,
    scraped_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (ticker, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_mimir_niche_prices_time ON yggdrasil.mimir_niche_prices (timestamp DESC);

CREATE TABLE IF NOT EXISTS yggdrasil.mimir_pair_signals (
    pair_id SERIAL PRIMARY KEY,
    ticker1 VARCHAR(50) NOT NULL REFERENCES yggdrasil.mimir_niche_assets(ticker),
    ticker2 VARCHAR(50) NOT NULL REFERENCES yggdrasil.mimir_niche_assets(ticker),
    signal_date TIMESTAMPTZ NOT NULL,
    z_score NUMERIC NOT NULL,
    p_value NUMERIC NOT NULL,
    status VARCHAR(50) DEFAULT 'OPEN',
    created_at TIMESTAMPTZ DEFAULT NOW()
);
"""

def main():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        print("Creating table yggdrasil.mimir_niche_assets, prices, and pair_signals...")
        cur.execute(CREATE_TABLES_SQL)
        conn.commit()
        print("[OK] Tables created successfully.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ERROR] Error creating tables: {e}")

if __name__ == "__main__":
    main()
