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

:: --- 3. Resolve Local Network IP for LAN / Mobile Devices ---
for /f "tokens=*" %%i in ('.venv\Scripts\python.exe -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8',80)); print(s.getsockname()[0]); s.close()" 2^>nul') do set LOCAL_IP=%%i

echo ===================================================
echo 🌳 MIMIR: Market Intelligence Reactor
echo ===================================================
echo 💻 Local Access:          http://127.0.0.1:8000
if defined LOCAL_IP (
    echo 📱 LAN / Network Access:   http://%LOCAL_IP%:8000
) else (
    echo 📱 LAN / Network Access:   http://^<YOUR_LOCAL_IP^>:8000
)
echo ===================================================
echo.

:: --- 4. Cloudflare Tunnel Detection & PATH Setup ---
set "PATH=%PATH%;C:\Program Files (x86)\cloudflared;C:\Program Files\cloudflared;%LocalAppData%\Microsoft\WinGet\Links"

where cloudflared >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [Cloudflare] 'cloudflared' was not found in PATH.
    echo [Cloudflare] Installing Cloudflare Tunnel automatically via winget...
    winget install Cloudflare.cloudflared --accept-package-agreements --accept-source-agreements
)

where cloudflared >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [Cloudflare] Starting Cloudflare HTTPS Tunnel in a dedicated terminal window...
    start "MIMIR Cloudflare HTTPS Tunnel" cmd /k "echo ================================================== & echo  MIMIR PUBLIC REMOTE HTTPS TUNNEL & echo ================================================== & echo Copy the https://xxxx.trycloudflare.com URL below to share with friends! & echo. & cloudflared tunnel --url http://localhost:8000"
) else (
    echo [Cloudflare WARNING] Unable to launch cloudflared automatically. You can install it via 'winget install Cloudflare.cloudflared'.
)

echo.
:: --- 5. Start Background Daemons in Dedicated Minimized Windows ---
:: (Prevents log pollution and avoids orphaned background zombie processes)
echo [1/3] Starting MT5 Live Price Fetcher (minimized)...
start "MIMIR - MT5 Price Fetcher" /min .venv\Scripts\python.exe scripts\mt5_price_fetcher.py

echo [2/3] Starting Live Price & Profit Ratchet Daemon (minimized)...
start "MIMIR - Live Price Daemon" /min .venv\Scripts\python.exe backend\app\pipeline\live_price_daemon.py

:: --- 6. Open Browser After Server Initializes ---
echo [3/3] Queueing browser launch for http://127.0.0.1:8000...
start "" /B cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:8000"

echo.
echo Starting backend uvicorn server (Listening on 0.0.0.0:8000)...
.venv\Scripts\uvicorn backend.app.main:app --reload --reload-dir backend --host 0.0.0.0 --port 8000

:: --- 7. Cleanup Daemons on Server Exit ---
echo.
echo [MIMIR] Uvicorn stopped. Terminating background fetchers...
taskkill /FI "WINDOWTITLE eq MIMIR - MT5 Price Fetcher" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq MIMIR - Live Price Daemon" /F >nul 2>&1
echo [MIMIR] All services cleanly stopped.
pause
