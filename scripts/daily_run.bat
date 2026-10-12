@echo off
rem 每日流水线启动器（供 Windows 任务计划调用；也可手动双击运行）
rem 用法与 scripts/daily_run.py 相同（如 --force / --backtest / --no-notify）
cd /d "%~dp0.."
".venv\Scripts\python.exe" "scripts\daily_run.py" %*
