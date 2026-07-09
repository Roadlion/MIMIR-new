# scripts/create_trade_signals_table.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {settings.mimir_schema}.mimir_trade_signals (
    id SERIAL PRIMARY KEY,
    ticker VARCHAR(20) NOT NULL,
    signal_type VARCHAR(10) NOT NULL, -- 'BUY' or 'SELL'
    trigger_price NUMERIC NOT NULL,
    rsi_value NUMERIC,
    sentiment_score NUMERIC,
    support_level NUMERIC,
    resistance_level NUMERIC,
    reason TEXT,
    status VARCHAR(20) DEFAULT 'PENDING', -- 'PENDING', 'APPROVED', 'REJECTED'
    created_at TIMESTAMPTZ DEFAULT NOW(),
    acted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_mimir_trade_signals_ticker ON {settings.mimir_schema}.mimir_trade_signals (ticker);
CREATE INDEX IF NOT EXISTS idx_mimir_trade_signals_status ON {settings.mimir_schema}.mimir_trade_signals (status);
"""

def main():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        print(f"Creating table {settings.mimir_schema}.mimir_trade_signals...")
        cur.execute(CREATE_TABLE_SQL)
        conn.commit()
        print("[OK] Trade signals table created successfully.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ERROR] Error creating table: {e}")

if __name__ == "__main__":
    main()
