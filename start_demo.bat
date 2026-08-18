@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set HF_HOME=%~dp0models\huggingface
set DOCLING_CACHE_DIR=%~dp0models\docling
set HF_HUB_DISABLE_SYMLINKS_WARNING=1
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -X utf8 app.py
) else (
  python -X utf8 app.py
)
pause
