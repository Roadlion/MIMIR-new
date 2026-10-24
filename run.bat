@echo off
title MIMIR Server Launcher

:: --- 1. Anchor Working Directory to Batch Location ---
cd /d "%~dp0"

:: --- 2. Auto-Detect or Activate Python Virtual Environment (.venv) ---
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment .venv\Scripts\python.exe was not found!
    pause
    exit /b 1
)

:: --- 2b. Auto-Install Missing Libraries & Ensure .env Exists ---
if exist "scripts\bootstrap_env.py" (
    ".venv\Scripts\python.exe" scripts\bootstrap_env.py
)

echo ===================================================
echo 🌳 MIMIR: Market Intelligence Reactor
echo ===================================================
echo.
echo [1/2] Opening browser to http://127.0.0.1:8000...
start "" http://127.0.0.1:8000

echo [2/2] Starting backend uvicorn server...
".venv\Scripts\python.exe" -m uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000

pause
