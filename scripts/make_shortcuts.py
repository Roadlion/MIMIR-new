# scripts/make_shortcuts.py
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
run_bat = str(ROOT / "run.bat")
stop_bat = str(ROOT / "stop.bat")
icon = str(ROOT / "MIMIR LOGO.ico")

ps_script = f'''
$ws = New-Object -ComObject WScript.Shell
$desk = [System.Environment]::GetFolderPath("Desktop")

$s1 = $ws.CreateShortcut((Join-Path $desk "Start MIMIR.lnk"))
$s1.TargetPath = "{run_bat}"
$s1.WorkingDirectory = "{str(ROOT)}"
if (Test-Path "{icon}") {{ $s1.IconLocation = "{icon}" }}
$s1.Description = "Start MIMIR Server & Daemons"
$s1.Save()

$s2 = $ws.CreateShortcut((Join-Path $desk "Turn Off MIMIR.lnk"))
$s2.TargetPath = "{stop_bat}"
$s2.WorkingDirectory = "{str(ROOT)}"
$s2.IconLocation = "shell32.dll,27"
$s2.Description = "Safely turn OFF all MIMIR engines and daemons"
$s2.Save()

Write-Output "[OK] Shortcuts created on Desktop: 'Start MIMIR' and 'Turn Off MIMIR'"
'''

res = subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], capture_output=True, text=True)
print(res.stdout)
if res.stderr:
    print(res.stderr)
