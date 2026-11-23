@echo off
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Project environment missing. See README.md.
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -X utf8 "%~dp0main.py" %*
