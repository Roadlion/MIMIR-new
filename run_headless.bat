@echo off
setlocal enabledelayedexpansion
title MIMIR Gamer Mode (Headless Background)

:: --- 1. Anchor Working Directory ---
cd /d "%~dp0"

:: --- 2. Verify Python Virtual Environment ---
if not exist ".venv\Scripts\python.exe" (
    echo ===================================================
    echo [ERROR] Virtual environment not found at .venv!
    echo ===================================================
    pause
    exit /b 1
)

:: --- 3. Pre-Startup Cleanup: Purge any lingering zombie processes ---
".venv\Scripts\python.exe" scripts\clean_shutdown.py --quiet >nul 2>&1
".venv\Scripts\python.exe" scripts\ensure_db_integrity.py

:: --- 4. Force Zero-VRAM / Gamer Mode Environment Flags ---
set MIMIR_DISABLE_GPU=1
set MIMIR_GAMER_MODE=1
set PYTHONUNBUFFERED=1
set CUDA_VISIBLE_DEVICES=

echo ===================================================
echo  [MIMIR GAMER MODE ACTIVE - ZERO-VRAM & LOW CPU]
echo ===================================================
echo  - CPU Scheduling:  BELOW_NORMAL (100%% CPU yields to games)
echo  - GPU / VRAM:      0 MB (RTX 3060 100%% dedicated to your game)
echo  - Memory Mode:     Headless (No browser rendering tabs)
echo  - Risk & Stops:    MT5 Trailing Stops ^& Ratchets Active
echo  - Alerts Channel:  Discord Notifications Active
echo.
echo  MIMIR is running in the background. You can game freely!
echo  To turn off MIMIR: Press Ctrl+C here or run 'stop.bat'.
echo ===================================================
echo.

:: --- 5. Start Background Daemons with BELOW_NORMAL Priority Minimized ---
echo [1/2] Starting MT5 Price Fetcher (low priority, minimized)...
start "MIMIR - MT5 Price Fetcher" /belownormal /min .venv\Scripts\python.exe scripts\mt5_price_fetcher.py

echo [2/2] Starting Live Price & Profit Ratchet Daemon (low priority, minimized)...
start "MIMIR - Live Price Daemon" /belownormal /min .venv\Scripts\python.exe backend\app\pipeline\live_price_daemon.py

:: --- 6. Start Uvicorn Server in Headless Production Mode (NO --reload, below-normal priority) ---
echo Starting backend server (Listening on 0.0.0.0:8000)...
start /b /belownormal .venv\Scripts\uvicorn.exe backend.app.main:app --host 0.0.0.0 --port 8000

:: Keep launcher console responsive for shutdown
:loop
timeout /t 5 /nobreak >nul
goto loop

:: --- Safe Cleanup on Exit ---
call "%~dp0stop.bat"
