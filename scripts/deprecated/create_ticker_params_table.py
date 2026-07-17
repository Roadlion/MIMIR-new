# scripts/create_ticker_params_table.py
import sys
from pathlib import Path

# Adjust path to import backend
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()

CREATE_PARAMS_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {settings.mimir_schema}.mimir_ticker_parameters (
    ticker VARCHAR(20) PRIMARY KEY,
    optimal_rsi_buy NUMERIC NOT NULL,
    optimal_rsi_sell NUMERIC NOT NULL DEFAULT 65.0,
    optimal_sentiment NUMERIC NOT NULL,
    optimal_vol_ratio NUMERIC NOT NULL,
    optimal_hold_days INT NOT NULL,
    win_rate NUMERIC,
    avg_pnl NUMERIC,
    total_trades INT,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
"""

ALTER_SIGNALS_TABLE_SQL = f"""
ALTER TABLE {settings.mimir_schema}.mimir_trade_signals 
ADD COLUMN IF NOT EXISTS evaluation_price NUMERIC,
ADD COLUMN IF NOT EXISTS evaluation_pnl_pct NUMERIC,
ADD COLUMN IF NOT EXISTS evaluation_status VARCHAR(20),
ADD COLUMN IF NOT EXISTS evaluated_at TIMESTAMPTZ;
"""

def main():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 1. Create ticker parameters table
        print(f"Creating table {settings.mimir_schema}.mimir_ticker_parameters...")
        cur.execute(CREATE_PARAMS_TABLE_SQL)
        
        # 2. Add evaluation columns to trade signals table
        print(f"Adding evaluation columns to {settings.mimir_schema}.mimir_trade_signals...")
        cur.execute(ALTER_SIGNALS_TABLE_SQL)
        
        conn.commit()
        print("[OK] Database migrations completed successfully.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ERROR] Error executing database migration: {e}")

if __name__ == "__main__":
    main()
