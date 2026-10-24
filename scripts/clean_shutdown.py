# scripts/clean_shutdown.py
"""
MIMIR Clean Process Purge & Safe Shutdown Script
Kills all background worker pipelines, MT5 fetchers, live price daemons,
Cloudflare tunnels, and server processes without leaving orphaned processes.
"""
import os
import sys
from pathlib import Path

# Safe utf-8 stream configuration for Windows console code pages (e.g. cp874)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.services.process_manager import stop_all_processes, disconnect_mt5, close_mimir_console_windows
from backend.app.services.db_integrity import sync_database_buffers


def main():
    quiet = "--quiet" in sys.argv
    if not quiet:
        print("===================================================")
        print("[MIMIR] Clean Process Purge & Safe Shutdown")
        print("===================================================")
        print("[1/4] Flushing database buffers to disk (CHECKPOINT)...")

    sync_database_buffers()

    if not quiet:
        print("[2/4] Disconnecting MetaTrader 5 terminal session...")

    disconnect_mt5()

    if not quiet:
        print("[3/4] Terminating MIMIR daemons, pipelines, and worker processes...")

    report = stop_all_processes(skip_current=False)

    if not quiet:
        tracked_count = len(report.get("tracked_pids_stopped", []))
        orphan_count = len(report.get("orphan_processes_killed", []))
        print(f"  - Tracked processes stopped: {tracked_count}")
        print(f"  - Lingering / orphan processes purged: {orphan_count}")
        print("[4/4] Closing any remaining MIMIR console windows...")

    close_mimir_console_windows()

    if not quiet:
        print("\n===================================================")
        print("[SUCCESS] All MIMIR engines safely turned OFF!")
        print("Computer CPU and memory resources are fully restored.")
        print("===================================================")


if __name__ == "__main__":
    main()
