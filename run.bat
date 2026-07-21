@echo off
title MIMIR Server Launcher
echo ===================================================
echo 🌳 MIMIR: Market Intelligence Reactor
echo ===================================================
echo.
echo [1/4] Starting MT5 Live Price Fetcher...
start "MIMIR MT5 Fetcher" cmd /k ".venv\Scripts\python.exe scripts\mt5_price_fetcher.py"

echo [2/4] Starting Live Event-Driven Daemon...
start "MIMIR Live Daemon" cmd /k ".venv\Scripts\python.exe backend\app\pipeline\live_price_daemon.py"

echo [3/4] Opening browser to http://127.0.0.1:8000...
start "" http://127.0.0.1:8000

echo [4/4] Starting backend server...
.venv\Scripts\uvicorn backend.app.main:app --reload --reload-dir backend --port 8000
pause
