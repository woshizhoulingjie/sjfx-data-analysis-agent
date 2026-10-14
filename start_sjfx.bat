@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if exist "runtime\python.exe" (
  "runtime\python.exe" -X utf8 sjfxctl.py %* start --open-browser
) else if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -X utf8 sjfxctl.py %* start --open-browser
) else (
  python -X utf8 sjfxctl.py %* start --open-browser
)
if errorlevel 1 pause
