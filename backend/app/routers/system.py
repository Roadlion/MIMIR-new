# backend/app/routers/system.py
"""
MIMIR System Management Router
Provides safe shutdown, status monitoring, and resource lifecycle controls.
"""
from fastapi import APIRouter, HTTPException, BackgroundTasks
import logging
from ..services.process_manager import (
    stop_all_processes,
    get_system_status,
    schedule_server_exit
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/status")
def system_status():
    """Returns current active status of MIMIR engines and daemons."""
    return get_system_status()


@router.post("/shutdown")
def system_shutdown(background_tasks: BackgroundTasks):
    """
    Safely shuts down MIMIR:
    1. Signals background workers to cancel in-flight tasks
    2. Gracefully disconnects MetaTrader 5 terminal
    3. Terminates live price daemons, scrapers, and Cloudflare tunnels
    4. Schedules server process termination after delivering this response
    """
    logger.info("[SYSTEM] Shutdown requested via Web UI.")
    try:
        # Stop background daemons, worker subprocesses, MT5, and tunnels
        report = stop_all_processes(skip_current=True)

        # Schedule the web server process itself to terminate 1.2s after returning response
        schedule_server_exit(delay=1.2)

        return {
            "status": "shutting_down",
            "message": "All MIMIR engines and background daemons have been safely powered down.",
            "report": report
        }
    except Exception as e:
        logger.error(f"[SYSTEM] Error during shutdown: {e}")
        # Even if error occurs, schedule exit so ports aren't blocked
        schedule_server_exit(delay=1.5)
        return {
            "status": "shutting_down_with_warnings",
            "message": f"Shutdown initiated with notice: {str(e)}"
        }
