# scripts/migrate_spillover_v2.py
"""
Migration script for MIMIR Spillover Engine Overhaul (Phase 1 & 2):
1. Creates backup archive table: mimir_asset_relationships_legacy_archive
2. Adds taxonomy columns: relationship_type, polarity, revenue_exposure_pct, lead_lag_hours, validation_source, economic_rationale
3. Creates indexes on active relationships, source/target keys, and polarity
4. Quarantines (deactivates) all 20,800+ spurious co-occurrence mined edges
5. Upgrades existing curated and sector relationships with typed metadata
"""

import sys
from pathlib import Path
import psycopg2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()
schema = settings.mimir_schema

def run_migration():
    print("=" * 80)
    print("  MIMIR SPILLOVER ENGINE OVERHAUL: PHASE 1 & 2 MIGRATION")
    print("  Schema Taxonomy Upgrade & Co-occurrence Noise Quarantine")
    print("=" * 80)

    conn = get_db_connection()
    cur = conn.cursor()

    try:
        # 1. Backup archive table
        print("\n[1/5] Backing up existing relationships table...")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {schema}.mimir_asset_relationships_legacy_archive AS
            SELECT * FROM {schema}.mimir_asset_relationships;
        """)
        conn.commit()
        cur.execute(f"SELECT COUNT(*) FROM {schema}.mimir_asset_relationships_legacy_archive;")
        archived_count = cur.fetchone()[0]
        print(f"      Successfully archived {archived_count} legacy relationship rows.")

        # 2. Add new columns
        print("\n[2/5] Upgrading schema with institutional taxonomy columns...")
        cur.execute(f"""
            ALTER TABLE {schema}.mimir_asset_relationships
                ADD COLUMN IF NOT EXISTS relationship_type VARCHAR(50) DEFAULT 'SUPPLIER',
                ADD COLUMN IF NOT EXISTS polarity NUMERIC(3,2) DEFAULT 1.00,
                ADD COLUMN IF NOT EXISTS revenue_exposure_pct NUMERIC(5,2) DEFAULT 10.00,
                ADD COLUMN IF NOT EXISTS lead_lag_hours INTEGER DEFAULT 24,
                ADD COLUMN IF NOT EXISTS validation_source VARCHAR(50) DEFAULT 'MANUAL_CURATED',
                ADD COLUMN IF NOT EXISTS economic_rationale TEXT;
        """)
        conn.commit()
        print("      Schema columns added successfully.")

        # 3. Create indexes
        print("\n[3/5] Creating performance indexes for fast graph traversal...")
        cur.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_rel_source_active 
                ON {schema}.mimir_asset_relationships (source_key, is_active);
            CREATE INDEX IF NOT EXISTS idx_rel_target_active 
                ON {schema}.mimir_asset_relationships (target_key, is_active);
            CREATE INDEX IF NOT EXISTS idx_rel_type_polarity 
                ON {schema}.mimir_asset_relationships (relationship_type, polarity);
        """)
        conn.commit()
        print("      Indexes verified.")

        # 4. Quarantine spurious co-occurrence edges
        print("\n[4/5] Quarantining (deactivating) spurious co-occurrence edges...")
        cur.execute(f"""
            UPDATE {schema}.mimir_asset_relationships
            SET is_active = FALSE,
                is_validated = FALSE,
                economic_rationale = 'Deactivated by Spillover Overhaul: unverified co-occurrence noise'
            WHERE discovery_source = 'co_occurrence' 
               OR chain_category = 'mined';
        """)
        quarantined_count = cur.rowcount
        conn.commit()
        print(f"      Quarantined {quarantined_count} spurious co-occurrence edges.")

        # 5. Upgrade existing curated and sector edges
        print("\n[5/5] Upgrading verified curated and sector relationships...")
        cur.execute(f"""
            UPDATE {schema}.mimir_asset_relationships
            SET relationship_type = 'SUPPLIER',
                polarity = 1.00,
                validation_source = 'MANUAL_CURATED',
                lead_lag_hours = COALESCE(diffusion_days * 24, 24)
            WHERE discovery_source = 'curated'
              AND (relationship_type IS NULL OR relationship_type = 'SUPPLIER');

            UPDATE {schema}.mimir_asset_relationships
            SET relationship_type = 'SECTOR_CONSTITUENT',
                polarity = 1.00,
                validation_source = 'SECTOR_MAP',
                lead_lag_hours = 0
            WHERE discovery_source = 'manual'
              AND chain_category = 'sector';
        """)
        upgraded_count = cur.rowcount
        conn.commit()
        print(f"      Upgraded {upgraded_count} curated and sector records.")

        # Final audit
        cur.execute(f"""
            SELECT is_active, COUNT(*)
            FROM {schema}.mimir_asset_relationships
            GROUP BY is_active;
        """)
        stats = cur.fetchall()
        print("\n[AUDIT] Active vs Quarantined Edges:")
        for is_act, cnt in stats:
            status_label = "ACTIVE (Clean/Curated)" if is_act else "QUARANTINED (Deactivated)"
            print(f"  • {status_label}: {cnt} rows")

        print("\n[SUCCESS] Phase 1 & 2 migration complete!")

    except Exception as e:
        conn.rollback()
        print(f"\n[ERROR] Migration failed: {e}")
        raise
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    run_migration()
