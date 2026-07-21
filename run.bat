@echo off
title MIMIR Server Launcher
echo ===================================================
echo 🌳 MIMIR: Market Intelligence Reactor
echo ===================================================
echo.
echo [1/3] Starting MT5 Live Price Fetcher in background...
start /B "" .venv\Scripts\python.exe scripts\mt5_price_fetcher.py

echo [2/3] Starting Live Event-Driven Daemon in background...
start /B "" .venv\Scripts\python.exe backend\app\pipeline\live_price_daemon.py

echo [3/3] Opening browser to http://127.0.0.1:8000...
start "" http://127.0.0.1:8000

echo.
echo Starting backend uvicorn server...
.venv\Scripts\uvicorn backend.app.main:app --reload --reload-dir backend --port 8000
pause
