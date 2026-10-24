# scripts/migrate_copilot_schema.py
"""
Migration script to initialize MIMIR Multi-User IRL Portfolio Co-Pilot schema.
Adds columns to yggdrasil.mimir_users and creates mimir_user_positions and mimir_user_trade_alerts.
"""

import sys
import os

# Add root directory to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.app.database import get_db_connection

def run_migration():
    print("[MIGRATION] Starting MIMIR Co-Pilot schema migration...")
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # 1. Update mimir_users
            print("[MIGRATION] Updating yggdrasil.mimir_users...")
            cur.execute("""
                ALTER TABLE yggdrasil.mimir_users 
                ADD COLUMN IF NOT EXISTS discord_id VARCHAR(64),
                ADD COLUMN IF NOT EXISTS discord_webhook_trades VARCHAR(512),
                ADD COLUMN IF NOT EXISTS total_budget NUMERIC(12, 2) DEFAULT 10000.00,
                ADD COLUMN IF NOT EXISTS dry_powder NUMERIC(12, 2) DEFAULT 10000.00,
                ADD COLUMN IF NOT EXISTS risk_per_trade_pct NUMERIC(4, 2) DEFAULT 1.00,
                ADD COLUMN IF NOT EXISTS max_position_pct NUMERIC(4, 2) DEFAULT 10.00,
                ADD COLUMN IF NOT EXISTS max_sector_pct NUMERIC(4, 2) DEFAULT 25.00,
                ADD COLUMN IF NOT EXISTS is_copilot_active BOOLEAN DEFAULT TRUE;
            """)

            # 2. Create mimir_user_positions
            print("[MIGRATION] Creating yggdrasil.mimir_user_positions...")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS yggdrasil.mimir_user_positions (
                    id SERIAL PRIMARY KEY,
                    user_id INT REFERENCES yggdrasil.mimir_users(id) ON DELETE CASCADE,
                    ticker VARCHAR(16) NOT NULL,
                    shares NUMERIC(12, 4) NOT NULL,
                    entry_price NUMERIC(12, 2) NOT NULL,
                    entry_time TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    current_stop_loss NUMERIC(12, 2) NOT NULL,
                    target_price NUMERIC(12, 2),
                    current_ratchet_level NUMERIC(12, 2),
                    holding_period_days INT DEFAULT 7,
                    sector VARCHAR(32),
                    status VARCHAR(16) DEFAULT 'OPEN',
                    close_price NUMERIC(12, 2),
                    close_time TIMESTAMP WITH TIME ZONE,
                    close_reason VARCHAR(32),
                    realized_pnl NUMERIC(12, 2),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_mimir_user_positions_active 
                ON yggdrasil.mimir_user_positions(user_id, status);
            """)

            # 3. Create mimir_user_trade_alerts
            print("[MIGRATION] Creating yggdrasil.mimir_user_trade_alerts...")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS yggdrasil.mimir_user_trade_alerts (
                    id SERIAL PRIMARY KEY,
                    user_id INT REFERENCES yggdrasil.mimir_users(id) ON DELETE CASCADE,
                    signal_id INT REFERENCES yggdrasil.mimir_trade_signals(id) ON DELETE SET NULL,
                    ticker VARCHAR(16) NOT NULL,
                    action VARCHAR(8) NOT NULL,
                    recommended_shares NUMERIC(12, 4) NOT NULL,
                    estimated_cost NUMERIC(12, 2) NOT NULL,
                    entry_price NUMERIC(12, 2) NOT NULL,
                    stop_loss NUMERIC(12, 2),
                    target_price NUMERIC(12, 2),
                    dry_powder_before NUMERIC(12, 2),
                    dry_powder_after NUMERIC(12, 2),
                    status VARCHAR(16) DEFAULT 'SENT',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                );
            """)

            conn.commit()
    print("[MIGRATION] Migration completed successfully!")

if __name__ == "__main__":
    run_migration()
