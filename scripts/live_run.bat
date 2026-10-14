@echo off
cd /d "%~dp0.."
".venv\Scripts\python.exe" "run_live.py" --real >> "logs\live_task.log" 2>&1
