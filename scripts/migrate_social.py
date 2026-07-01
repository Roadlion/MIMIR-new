# scripts/migrate_social.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from backend.app.database import get_db_connection

def main():
    migration_file = Path(__file__).parent / "create_social_chatter_table.sql"
    print(f"Reading migration from {migration_file}...")
    
    try:
        with open(migration_file, "r", encoding="utf-8") as f:
            sql = f.read()
            
        conn = get_db_connection()
        cur = conn.cursor()
        print("Executing database migration...")
        cur.execute(sql)
        conn.commit()
        print("[OK] Social sentiment database migration completed successfully.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ERROR] Migration failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
