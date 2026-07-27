# scripts/migrate_db_supply_chains.py
import sys
import os
from pathlib import Path
import psycopg2
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))
dotenv_path = Path(__file__).parent.parent / '.env'
load_dotenv(dotenv_path)

DB_URL = f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
SCHEMA = os.getenv('MIMIR_SCHEMA', 'yggdrasil')

MIGRATION_SQL = f"""
-- Ensure relationships table exists with standard columns
CREATE TABLE IF NOT EXISTS {SCHEMA}.mimir_asset_relationships (
    id SERIAL PRIMARY KEY,
    source_type VARCHAR(50) DEFAULT 'ticker',
    source_key VARCHAR(50) NOT NULL,
    target_type VARCHAR(50) DEFAULT 'ticker',
    target_key VARCHAR(50) NOT NULL,
    decay_factor NUMERIC(4,3) DEFAULT 0.800,
    relationship_type VARCHAR(50) DEFAULT 'supply_chain',
    is_active BOOLEAN DEFAULT TRUE,
    created_ts TIMESTAMPTZ DEFAULT NOW()
);

-- Add missing columns
ALTER TABLE {SCHEMA}.mimir_asset_relationships
  ADD COLUMN IF NOT EXISTS source_key VARCHAR(50),
  ADD COLUMN IF NOT EXISTS target_key VARCHAR(50),
  ADD COLUMN IF NOT EXISTS source_type VARCHAR(50) DEFAULT 'ticker',
  ADD COLUMN IF NOT EXISTS target_type VARCHAR(50) DEFAULT 'ticker',
  ADD COLUMN IF NOT EXISTS decay_factor NUMERIC(4,3) DEFAULT 0.800,
  ADD COLUMN IF NOT EXISTS chain_tier INTEGER DEFAULT 1,
  ADD COLUMN IF NOT EXISTS diffusion_days INTEGER DEFAULT 0,
  ADD COLUMN IF NOT EXISTS chain_category VARCHAR(50) DEFAULT 'sector',
  ADD COLUMN IF NOT EXISTS discovery_source VARCHAR(20) DEFAULT 'manual',
  ADD COLUMN IF NOT EXISTS is_validated BOOLEAN DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS confidence_score NUMERIC(4,3) DEFAULT 0.500,
  ADD COLUMN IF NOT EXISTS last_validated TIMESTAMPTZ;

-- Add activation_date to mimir_sentiment_impacts
ALTER TABLE {SCHEMA}.mimir_sentiment_impacts
  ADD COLUMN IF NOT EXISTS activation_date TIMESTAMPTZ;
"""

def run_migration():
    print(f"Connecting to database to run Project Odin schema migration...")
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        cur.execute(MIGRATION_SQL)
        conn.commit()
        print("Successfully applied Project Odin schema migration!")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Migration error: {e}")

if __name__ == "__main__":
    run_migration()
