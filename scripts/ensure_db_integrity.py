# scripts/ensure_db_integrity.py
"""
MIMIR Database Integrity & Self-Healing Pre-flight Check
Verifies PostgreSQL tables and automatically repairs any damaged blocks before startup.
"""
import sys
import os
from pathlib import Path

# Fix console encoding on Windows
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.services.db_integrity import ensure_database_integrity, sync_database_buffers


def main():
    print("===================================================")
    print(" [MIMIR] Database Integrity & Auto-Healing Check")
    print("===================================================")
    try:
        report = ensure_database_integrity()
        print(f" Checked {report['checked']} core database tables.")
        if report['failed']:
            print(f" [WARNING] Could not repair tables: {report['failed']}")
            sys.exit(1)
        else:
            print(" [OK] All core database tables are healthy and readable.")
            sync_database_buffers()
            print("===================================================")
    except Exception as e:
        print(f" [NOTE] Pre-flight DB check note: {e}")
        # Don't halt entire startup if DB is simply starting up
        sys.exit(0)


if __name__ == "__main__":
    main()
