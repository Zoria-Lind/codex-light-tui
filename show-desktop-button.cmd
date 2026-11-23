@echo off
setlocal
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
  echo Project environment missing. See README.md.
  exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" -X utf8 "%~dp0handoff_overlay.py"
