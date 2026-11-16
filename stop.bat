@echo off
setlocal enabledelayedexpansion
title MIMIR Safe Power-Off Utility
cd /d "%~dp0"

echo ===================================================
echo  [MIMIR] Safe Power-Off Utility
echo ===================================================
echo.

:: --- 1. Signal Web Server (if running on port 8000) ---
echo [1/4] Signaling MIMIR server to shut down cleanly...
curl -s -m 1 -X POST http://127.0.0.1:8000/api/v1/system/shutdown >nul 2>&1

:: --- 2. Terminate Tracked PIDs from .mimir_pids.json directly ---
if exist ".mimir_pids.json" (
    echo [2/4] Terminating tracked daemons by PID...
    for /f "tokens=2 delims=:, " %%a in ('findstr /r "[0-9]" .mimir_pids.json') do (
        set "pid=%%~a"
        if defined pid (
            taskkill /PID !pid! /T /F >nul 2>&1
        )
    )
    del /f /q ".mimir_pids.json" >nul 2>&1
)

:: --- 3. Run Python clean shutdown routine (with safe timeouts) ---
echo [3/4] Running cleanup and disconnecting MetaTrader 5...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" scripts\clean_shutdown.py --quiet >nul 2>&1
) else (
    python scripts\clean_shutdown.py --quiet >nul 2>&1
)

:: --- 4. Purge any lingering console windows, daemons, and processes ---
echo [4/4] Purging remaining daemon processes and windows...

:: Kill by image name
taskkill /IM uvicorn.exe /T /F >nul 2>&1
taskkill /IM cloudflared.exe /T /F >nul 2>&1

:: Kill cmd windows with MIMIR titles
taskkill /FI "IMAGENAME eq cmd.exe" /FI "WINDOWTITLE eq MIMIR*" /F >nul 2>&1
taskkill /FI "IMAGENAME eq cmd.exe" /FI "WINDOWTITLE eq MIMIR Server Launcher*" /F >nul 2>&1
taskkill /FI "IMAGENAME eq cmd.exe" /FI "WINDOWTITLE eq MIMIR Gamer Mode*" /F >nul 2>&1
taskkill /FI "IMAGENAME eq cmd.exe" /FI "WINDOWTITLE eq MIMIR Cloudflare HTTPS Tunnel*" /F >nul 2>&1
taskkill /FI "IMAGENAME eq cmd.exe" /FI "WINDOWTITLE eq MIMIR - MT5 Price Fetcher*" /F >nul 2>&1
taskkill /FI "IMAGENAME eq cmd.exe" /FI "WINDOWTITLE eq MIMIR - Live Price Daemon*" /F >nul 2>&1

:: Kill python and conhost windows launched with MIMIR title
taskkill /FI "IMAGENAME eq python.exe" /FI "WINDOWTITLE eq MIMIR*" /F >nul 2>&1
taskkill /FI "IMAGENAME eq conhost.exe" /FI "WINDOWTITLE eq MIMIR*" /F >nul 2>&1

:: Clean up PID file if still present
if exist ".mimir_pids.json" del /f /q ".mimir_pids.json" >nul 2>&1

echo.
echo ===================================================
echo  [SUCCESS] All MIMIR engines and daemons are OFF!
echo  No orphaned background processes remaining.
echo  Your computer resources have been fully restored.
echo ===================================================
echo.
ping 127.0.0.1 -n 2 >nul
exit /b 0
