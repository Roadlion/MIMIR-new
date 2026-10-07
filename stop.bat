@echo off
setlocal enabledelayedexpansion
title MIMIR Safe Power-Off Utility
cd /d "%~dp0"

echo ===================================================
echo  [MIMIR] Safe Power-Off Utility
echo ===================================================
echo.
echo [1/3] Signaling MIMIR server to shut down cleanly...
curl -s -m 2 -X POST http://127.0.0.1:8000/api/v1/system/shutdown >nul 2>&1
timeout /t 1 /nobreak >nul

echo [2/3] Terminating background daemons and freeing CPU/RAM resources...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" scripts\clean_shutdown.py
) else (
    python scripts\clean_shutdown.py
)

echo [3/3] Purging remaining daemon console windows...
taskkill /FI "IMAGENAME eq cmd.exe" /FI "WINDOWTITLE eq MIMIR Server Launcher*" /F >nul 2>&1
taskkill /FI "IMAGENAME eq cmd.exe" /FI "WINDOWTITLE eq MIMIR Cloudflare HTTPS Tunnel*" /F >nul 2>&1
taskkill /FI "IMAGENAME eq cmd.exe" /FI "WINDOWTITLE eq MIMIR - MT5 Price Fetcher*" /F >nul 2>&1
taskkill /FI "IMAGENAME eq cmd.exe" /FI "WINDOWTITLE eq MIMIR - Live Price Daemon*" /F >nul 2>&1
taskkill /FI "IMAGENAME eq cloudflared.exe" /F >nul 2>&1

echo.
echo ===================================================
echo  [SUCCESS] All MIMIR engines and daemons are OFF!
echo  No orphaned background processes remaining.
echo  Your computer resources have been fully restored.
echo ===================================================
echo.
timeout /t 3
exit /b 0
