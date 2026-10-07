@echo off
setlocal enabledelayedexpansion
title Create MIMIR Desktop Shortcuts
cd /d "%~dp0"

echo ===================================================
echo  MIMIR Desktop Shortcut Generator
echo ===================================================
echo.
echo Creating Desktop shortcuts for Start and Stop (OFF Button)...

set "ROOT_DIR=%~dp0"
:: Strip trailing backslash
if "%ROOT_DIR:~-1%"=="\" set "ROOT_DIR=%ROOT_DIR:~0,-1%"

set "RUN_BAT=%ROOT_DIR%\run.bat"
set "STOP_BAT=%ROOT_DIR%\stop.bat"
set "ICON_PATH=%ROOT_DIR%\MIMIR LOGO.ico"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ws = New-Object -ComObject WScript.Shell; " ^
    "$desk = [System.Environment]::GetFolderPath('Desktop'); " ^
    "$s1 = $ws.CreateShortcut(\"$desk\Start MIMIR.lnk\"); " ^
    "$s1.TargetPath = '%RUN_BAT%'; " ^
    "$s1.WorkingDirectory = '%ROOT_DIR%'; " ^
    "if (Test-Path '%ICON_PATH%') { $s1.IconLocation = '%ICON_PATH%'; } " ^
    "$s1.Description = 'Start MIMIR Server & Daemons'; " ^
    "$s1.Save(); " ^
    "$s2 = $ws.CreateShortcut(\"$desk\Stop MIMIR (OFF Button).lnk\"); " ^
    "$s2.TargetPath = '%STOP_BAT%'; " ^
    "$s2.WorkingDirectory = '%ROOT_DIR%'; " ^
    "$s2.IconLocation = 'shell32.dll,27'; " ^
    "$s2.Description = 'Safely turn OFF all MIMIR engines and daemons'; " ^
    "$s2.Save(); " ^
    "Write-Output '[OK] Shortcuts created successfully on Desktop.'"

echo.
echo ===================================================
echo  [SUCCESS] Created on Desktop:
echo  1. "Start MIMIR"
echo  2. "Stop MIMIR (OFF Button)"
echo ===================================================
echo.
pause
