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

