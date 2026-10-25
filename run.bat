@echo off
setlocal enabledelayedexpansion
title MIMIR Server Launcher

:: --- 1. Anchor Working Directory to Batch Location ---
cd /d "%~dp0"

:: --- 2. Verify Python Virtual Environment ---
if not exist ".venv\Scripts\python.exe" (
    echo ===================================================
    echo [ERROR] Virtual environment not found at .venv!
    echo Please ensure the .venv folder exists in this directory.
    echo ===================================================
    pause
    exit /b 1
)

:: --- 2b. Auto-Install Missing Libraries & Ensure .env Exists ---
if exist "scripts\bootstrap_env.py" (
    ".venv\Scripts\python.exe" scripts\bootstrap_env.py
)

:: --- 3. Resolve Local Network IP for LAN / Mobile Devices ---
for /f "tokens=*" %%i in ('.venv\Scripts\python.exe -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8',80)); print(s.getsockname()[0]); s.close()" 2^>nul') do set LOCAL_IP=%%i

echo ===================================================
echo  [MIMIR] Market Intelligence Reactor
echo ===================================================
echo  Local Access:          http://127.0.0.1:8000
if defined LOCAL_IP (
    echo  LAN / Network Access:   http://%LOCAL_IP%:8000
)
echo ===================================================
echo  Default Logins:
echo   Username: admin
echo   Password: admin (or any password in offline mode)
echo ===================================================
echo.

:: --- 4. Start Background Daemons Minimized (if present) ---
if exist "scripts\mt5_price_fetcher.py" (
    echo [*] Starting MT5 Price Fetcher [minimized]...
    start "MIMIR - MT5 Price Fetcher" /min .venv\Scripts\python.exe scripts\mt5_price_fetcher.py
)

if exist "backend\app\pipeline\live_price_daemon.py" (
    echo [*] Starting Live Price Daemon [minimized]...
    start "MIMIR - Live Price Daemon" /min .venv\Scripts\python.exe backend\app\pipeline\live_price_daemon.py
)

:: --- 5. Open Browser After Server Initializes ---
echo [*] Opening browser to http://127.0.0.1:8000...
start "" cmd /c "ping 127.0.0.1 -n 4 >nul & start http://127.0.0.1:8000"

echo.
echo Starting MIMIR backend server on http://0.0.0.0:8000 ...
echo (Keep this window open while using MIMIR. Press Ctrl+C to stop.)
echo.
".venv\Scripts\python.exe" -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000

echo.
echo ===================================================
echo [MIMIR] Server stopped.
echo ===================================================
pause
