@echo off
setlocal enabledelayedexpansion
title MIMIR Gamer Mode (Headless Background)

:: --- 1. Anchor Working Directory ---
cd /d "%~dp0"

:: --- 2. Auto-Detect or Create Python Virtual Environment (.venv) ---
set "VENV_VALID=0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys" >nul 2>&1
    if !ERRORLEVEL! EQU 0 set "VENV_VALID=1"
)

if "!VENV_VALID!"=="0" (
    echo ===================================================
    echo  [SETUP] Preparing Python Virtual Environment...
    echo ===================================================
    if exist ".venv" rmdir /s /q ".venv" >nul 2>&1

    set "SYS_PY="
    where py >nul 2>&1
    if !ERRORLEVEL! EQU 0 (
        py -3 --version >nul 2>&1
        if !ERRORLEVEL! EQU 0 set "SYS_PY=py -3"
    )
    if not defined SYS_PY (
        where python >nul 2>&1
        if !ERRORLEVEL! EQU 0 (
            python --version >nul 2>&1
            if !ERRORLEVEL! EQU 0 set "SYS_PY=python"
        )
    )
    if not defined SYS_PY (
        for /d %%D in ("%LocalAppData%\Programs\Python\Python3*" "C:\Program Files\Python3*") do (
            if exist "%%~fD\python.exe" set "SYS_PY="%%~fD\python.exe""
        )
    )

    if not defined SYS_PY (
        echo [ERROR] Python 3 was not found on this PC!
        echo Please install Python 3.10+ from https://www.python.org/downloads/
        pause
        exit /b 1
    )

    echo  Creating .venv using !SYS_PY!...
    !SYS_PY! -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo [ERROR] Failed to create virtual environment at .venv!
        pause
        exit /b 1
    )
)

:: --- 2b. Auto-Install Missing Libraries & Ensure .env Exists ---
".venv\Scripts\python.exe" scripts\bootstrap_env.py

:: --- 3. Pre-Startup Cleanup: Purge any lingering zombie processes ---
".venv\Scripts\python.exe" scripts\clean_shutdown.py --quiet >nul 2>&1
".venv\Scripts\python.exe" scripts\ensure_db_integrity.py

:: --- 4. Force Zero-VRAM / Gamer Mode Environment Flags ---
set MIMIR_DISABLE_GPU=1
set MIMIR_GAMER_MODE=1
set PYTHONUNBUFFERED=1
set CUDA_VISIBLE_DEVICES=

echo ===================================================
echo  [MIMIR GAMER MODE ACTIVE - ZERO-VRAM ^& LOW CPU]
echo ===================================================
echo  - CPU Scheduling:  BELOW_NORMAL (100%% CPU yields to games)
echo  - GPU / VRAM:      0 MB (RTX 3060 100%% dedicated to your game)
echo  - Memory Mode:     Headless (No browser rendering tabs)
echo  - Risk ^& Stops:    MT5 Trailing Stops ^& Ratchets Active
echo  - Alerts Channel:  Discord Notifications Active
echo.
echo  MIMIR is running in the background. You can game freely!
echo  To turn off MIMIR: Press Ctrl+C here or run 'stop.bat'.
echo ===================================================
echo.

:: --- 5. Start Background Daemons with BELOW_NORMAL Priority Minimized ---
echo [1/2] Starting MT5 Price Fetcher (low priority, minimized)...
start "MIMIR - MT5 Price Fetcher" /belownormal /min .venv\Scripts\python.exe scripts\mt5_price_fetcher.py

echo [2/2] Starting Live Price ^& Profit Ratchet Daemon (low priority, minimized)...
start "MIMIR - Live Price Daemon" /belownormal /min .venv\Scripts\python.exe backend\app\pipeline\live_price_daemon.py

:: --- 6. Start Uvicorn Server in Headless Production Mode (NO --reload, below-normal priority) ---
echo Starting backend server (Listening on 0.0.0.0:8000)...
start /b /belownormal "" ".venv\Scripts\python.exe" -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000

:: Keep launcher console responsive for shutdown
:loop
ping 127.0.0.1 -n 6 >nul
goto loop

:: --- Safe Cleanup on Exit ---
call "%~dp0stop.bat"
