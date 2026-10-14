@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONDONTWRITEBYTECODE=1
if exist "runtime\python.exe" (
  "runtime\python.exe" -B -X utf8 sjfxctl.py start --open-browser %*
) else if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -B -X utf8 sjfxctl.py start --open-browser %*
) else (
  python -B -X utf8 sjfxctl.py start --open-browser %*
)
if errorlevel 1 pause
