@echo off
title MIMIR Server Launcher
echo ===================================================
echo 🌳 MIMIR: Market Intelligence Reactor
echo ===================================================
echo.
echo [1/2] Opening browser to http://127.0.0.1:8000...
start "" http://127.0.0.1:8000

echo [2/2] Starting backend server...
.venv\Scripts\uvicorn backend.app.main:app --reload --reload-dir backend --port 8000
pause
