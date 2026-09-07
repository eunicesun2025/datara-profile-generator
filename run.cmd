@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
where uv >nul 2>nul
if errorlevel 1 (
  echo Install uv first: https://docs.astral.sh/uv/getting-started/installation/
  pause
  exit /b 1
)
uv sync --frozen
if errorlevel 1 (
  pause
  exit /b 1
)
echo Datara Profile Generator: http://127.0.0.1:8765
uv run --frozen python -m uvicorn datara.app:app --host 127.0.0.1 --port 8765
if errorlevel 1 pause
