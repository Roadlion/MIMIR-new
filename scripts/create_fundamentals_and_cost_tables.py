# scripts/create_fundamentals_and_cost_tables.py
import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()

def main():
    print(f"[MIGRATION] Connecting to database schema: {settings.mimir_schema}...")
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        # 1. Create fundamentals table
        print("[MIGRATION] Creating mimir_asset_fundamentals table...")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {settings.mimir_schema}.mimir_asset_fundamentals (
                ticker VARCHAR(12) PRIMARY KEY,
                pe_ratio NUMERIC,
                debt_to_equity NUMERIC,
                eps_growth NUMERIC,
                operating_margin NUMERIC,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
        """)
        
        # 2. Create cost ledger table
        print("[MIGRATION] Creating mimir_api_cost_ledger table...")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {settings.mimir_schema}.mimir_api_cost_ledger (
                id SERIAL PRIMARY KEY,
                service_name VARCHAR(50) NOT NULL,
                tokens_prompt INT DEFAULT 0,
                tokens_completion INT DEFAULT 0,
                cost_usd NUMERIC(10, 6) DEFAULT 0.0,
                item_count INT DEFAULT 1,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );
        """)
        
        conn.commit()
        print("[MIGRATION] Tables created successfully.")
    except Exception as e:
        conn.rollback()
        print(f"[MIGRATION] ERROR: {e}")
        sys.exit(1)
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    main()
