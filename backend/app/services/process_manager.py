# backend/app/services/process_manager.py
"""
MIMIR Process & Daemon Manager
Handles safe startup, PID tracking, and graceful system-wide shutdown
to prevent orphaned background zombie processes and computer freezes.
"""
import os
import sys
import json
import time
import logging
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Any, Set
import psutil

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
PID_FILE = PROJECT_ROOT / ".mimir_pids.json"

# Exact console window titles used by MIMIR launchers
MIMIR_CONSOLE_TITLES = [
    "MIMIR Server Launcher*",
    "MIMIR Cloudflare HTTPS Tunnel*",
    "MIMIR - MT5 Price Fetcher*",
    "MIMIR - Live Price Daemon*"
]

# Scripts that should be stopped during MIMIR shutdown
MIMIR_SCRIPTS = [
    "mt5_price_fetcher.py",
    "live_price_daemon.py",
    "tune_ticker_parameters.py",
    "run_full_pipeline",
    "push_to_db.py",
    "scrape_social.py",
    "scrape_twitter.py",
    "fetch_fundamentals.py",
    "run_price_fetch.py",
    "run_server.py"
]


def get_pid_file() -> Path:
    return PID_FILE


def load_pids() -> Dict[str, int]:
    if not PID_FILE.exists():
        return {}
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to read PID file: {e}")
        return {}


def save_pids(pids: Dict[str, int]) -> None:
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            json.dump(pids, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to write PID file: {e}")


def record_pid(name: str, pid: int) -> None:
    pids = load_pids()
    pids[name] = pid
    save_pids(pids)


def clear_pid_file() -> None:
    if PID_FILE.exists():
        try:
            PID_FILE.unlink()
        except Exception:
            pass


def get_current_ancestor_pids() -> Set[int]:
    """Returns the set of PIDs containing the current process and all its ancestors."""
    pids = set()
    try:
        curr = psutil.Process(os.getpid())
        pids.add(curr.pid)
        p = curr.parent()
        while p:
            pids.add(p.pid)
            try:
                p = p.parent()
            except Exception:
                break
    except Exception:
        pids.add(os.getpid())
    return pids


def disconnect_mt5():
    """Safely disconnect MetaTrader 5 terminal session."""
    try:
        import MetaTrader5 as mt5
        logger.info("[PROCESS_MGR] Disconnecting MetaTrader 5 session...")
        mt5.shutdown()
        logger.info("[PROCESS_MGR] MetaTrader 5 session cleanly closed.")
    except Exception as e:
        logger.debug(f"[PROCESS_MGR] MT5 shutdown note: {e}")


def stop_background_workers():
    """Signals all background worker threads in the FastAPI app to stop immediately."""
    try:
        from ..pipeline.background_worker import stop_background_worker
        stop_background_worker()
        logger.info("[PROCESS_MGR] Background worker loops signaled to stop.")
    except Exception as e:
        logger.debug(f"[PROCESS_MGR] Could not stop background workers: {e}")


def kill_process_tree(pid: int, timeout: float = 1.5) -> bool:
    """Terminates a process and all of its descendants safely."""
    try:
        if not psutil.pid_exists(pid):
            return True
        parent = psutil.Process(pid)
        
        # NEVER kill IDE, language servers, or system processes
        p_name = parent.name().lower()
        if "antigravity" in p_name or "language_server" in p_name or "explorer" in p_name:
            return False

        children = parent.children(recursive=True)
        for child in children:
            try:
                c_name = child.name().lower()
                if "antigravity" not in c_name and "language_server" not in c_name:
                    child.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        parent.terminate()

        gone, alive = psutil.wait_procs(children + [parent], timeout=timeout)
        for proc in alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True
    except Exception as e:
        logger.warning(f"[PROCESS_MGR] Error killing process tree {pid}: {e}")
        return False


def find_mimir_processes(exclude_pids: Optional[set] = None) -> List[psutil.Process]:
    """Finds all running processes associated with MIMIR (excluding caller process and its ancestors)."""
    exclude = set(exclude_pids or [])
    exclude.update(get_current_ancestor_pids())

    found = []
    root_str = str(PROJECT_ROOT).lower()

    for p in psutil.process_iter(['pid', 'name', 'exe', 'cmdline']):
        try:
            pid = p.info['pid']
            if pid in exclude:
                continue

            p_name = (p.info['name'] or '').lower()
            p_exe = (p.info['exe'] or '').lower()
            cmdline_list = p.info['cmdline'] or []
            cmdline_str = " ".join(cmdline_list).lower()

            # NEVER touch Antigravity IDE, language server, or other editors
            if any(x in p_name or x in p_exe for x in ['antigravity', 'language_server', 'code.exe', 'explorer.exe']):
                continue

            # NEVER touch the cleanup script itself
            if "clean_shutdown.py" in cmdline_str:
                continue

            # 1. Match Cloudflare tunnel running for MIMIR port 8000
            if "cloudflared" in p_name or "cloudflared" in p_exe:
                if "8000" in cmdline_str or "tunnel" in cmdline_str:
                    found.append(p)
                    continue

            # 2. Match Python processes running MIMIR scripts or uvicorn
            if "python" in p_name:
                # Check for explicit MIMIR scripts
                if any(script in cmdline_str for script in MIMIR_SCRIPTS):
                    found.append(p)
                    continue

                # Check if running uvicorn for MIMIR
                if "backend.app.main:app" in cmdline_str:
                    found.append(p)
                    continue

                # Check if running python directly from .venv (but not our own cleanup/test script)
                if root_str in p_exe and "clean_shutdown" not in cmdline_str and "-c" not in cmdline_list:
                    found.append(p)
                    continue

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return found


def close_mimir_console_windows():
    """Safely closes ONLY cmd.exe console windows matching MIMIR specific titles."""
    if sys.platform != "win32":
        return
    for title in MIMIR_CONSOLE_TITLES:
        try:
            cmd = f'taskkill /FI "IMAGENAME eq cmd.exe" /FI "WINDOWTITLE eq {title}" /F >nul 2>&1'
            subprocess.run(cmd, shell=True)
        except Exception:
            pass


def stop_all_processes(skip_current: bool = True) -> Dict[str, Any]:
    """
    Comprehensive clean termination of all MIMIR daemons, processes, and tunnels.
    Returns status report.
    """
    report = {
        "mt5_closed": False,
        "workers_stopped": False,
        "tracked_pids_stopped": [],
        "orphan_processes_killed": [],
        "windows_closed": False,
        "status": "success"
    }

    # 1. Gracefully stop workers & MT5
    try:
        stop_background_workers()
        report["workers_stopped"] = True
    except Exception:
        pass

    try:
        disconnect_mt5()
        report["mt5_closed"] = True
    except Exception:
        pass

    # Flush database buffers to disk to prevent torn pages/corruption
    try:
        from .db_integrity import sync_database_buffers
        sync_database_buffers()
    except Exception:
        pass

    # 2. Exclude current process and ancestors
    exclude = get_current_ancestor_pids()
    if skip_current:
        # Also exclude server parent/reloader
        try:
            curr = psutil.Process(os.getpid())
            parent = curr.parent()
            if parent:
                exclude.add(parent.pid)
        except Exception:
            pass

    # 3. Stop tracked PIDs from .mimir_pids.json
    tracked = load_pids()
    for name, pid in tracked.items():
        if pid in exclude:
            continue
        if kill_process_tree(pid, timeout=1.0):
            report["tracked_pids_stopped"].append({"name": name, "pid": pid})

    # 4. Terminate any orphan or untracked MIMIR processes
    orphans = find_mimir_processes(exclude_pids=exclude)
    for proc in orphans:
        try:
            pid = proc.pid
            name = proc.name()
            kill_process_tree(pid, timeout=1.0)
            report["orphan_processes_killed"].append({"name": name, "pid": pid})
        except Exception:
            pass

    # 5. Targeted Windows console cleanup (ONLY cmd.exe with MIMIR titles)
    close_mimir_console_windows()
    report["windows_closed"] = True

    clear_pid_file()
    return report


def get_system_status() -> Dict[str, Any]:
    """Check running status of all MIMIR system components."""
    tracked = load_pids()
    mimir_procs = find_mimir_processes()

    server_running = False
    mt5_running = False
    live_daemon_running = False
    cloudflared_running = False

    for p in mimir_procs + ([psutil.Process(os.getpid())] if psutil.pid_exists(os.getpid()) else []):
        try:
            cmd = " ".join(p.cmdline()).lower()
            name = p.name().lower()
            if "backend.app.main:app" in cmd or "uvicorn" in cmd:
                server_running = True
            if "mt5_price_fetcher.py" in cmd:
                mt5_running = True
            if "live_price_daemon.py" in cmd:
                live_daemon_running = True
            if "cloudflared" in name or "cloudflared" in cmd:
                cloudflared_running = True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    return {
        "server": server_running,
        "mt5_fetcher": mt5_running,
        "live_price_daemon": live_daemon_running,
        "cloudflared": cloudflared_running,
        "total_active_processes": len(mimir_procs) + (1 if server_running else 0),
        "tracked_pids": tracked
    }


def schedule_server_exit(delay: float = 1.0):
    """Schedules the current server process to exit after delay seconds, allowing HTTP response to finish."""
    import threading

    def _do_exit():
        time.sleep(delay)
        logger.info("[PROCESS_MGR] Initiating server process termination...")
        try:
            curr = psutil.Process(os.getpid())
            parent = curr.parent()
            if parent and "python" in parent.name().lower():
                try:
                    parent.terminate()
                except Exception:
                    pass
            curr.terminate()
        except Exception:
            pass
        os._exit(0)

    t = threading.Thread(target=_do_exit, daemon=True)
    t.start()
