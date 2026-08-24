@echo off
title MIMIR Server Launcher
for /f "tokens=*" %%i in ('.venv\Scripts\python.exe -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('8.8.8.8',80)); print(s.getsockname()[0]); s.close()" 2^>nul') do set LOCAL_IP=%%i

echo ===================================================
echo 🌳 MIMIR: Market Intelligence Reactor
echo ===================================================
echo 💻 Laptop Access:          http://127.0.0.1:8000
if defined LOCAL_IP (
echo 📱 iPad / Network Access:   http://%LOCAL_IP%:8000
) else (
echo 📱 iPad / Network Access:   http://^<YOUR_LAPTOP_IP^>:8000
)
echo ===================================================
echo.

:: --- Cloudflare Tunnel Check & Auto-Install ---
where cloudflared >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [Cloudflare] 'cloudflared' was not found in PATH.
    echo [Cloudflare] Installing Cloudflare Tunnel automatically via winget...
    winget install Cloudflare.cloudflared --accept-package-agreements --accept-source-agreements
    
    :: Add WinGet links path and standard install locations to PATH for current session
    set "PATH=%PATH%;%LocalAppData%\Microsoft\WinGet\Links;C:\Program Files (x86)\cloudflared;C:\Program Files\cloudflared"
)

where cloudflared >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [Cloudflare] Starting Cloudflare HTTPS Tunnel in a dedicated terminal window...
    start "MIMIR Cloudflare HTTPS Tunnel" cmd /k "echo ================================================== & echo  MIMIR PUBLIC REMOTE HTTPS TUNNEL & echo ================================================== & echo Copy the https://xxxx.trycloudflare.com URL below to share with friends! & echo. & cloudflared tunnel --url http://localhost:8000"
) else (
    echo [Cloudflare WARNING] Unable to launch cloudflared automatically. You can manually install it via 'winget install Cloudflare.cloudflared'.
)

echo.
echo [1/3] Starting MT5 Live Price Fetcher in background...
start /B "" .venv\Scripts\python.exe scripts\mt5_price_fetcher.py

echo [2/3] Starting Live Event-Driven Daemon in background...
start /B "" .venv\Scripts\python.exe backend\app\pipeline\live_price_daemon.py

echo [3/3] Opening browser on laptop...
start "" http://127.0.0.1:8000

echo.
echo Starting backend uvicorn server (Listening on 0.0.0.0:8000)...
.venv\Scripts\uvicorn backend.app.main:app --reload --reload-dir backend --host 0.0.0.0 --port 8000
pause
