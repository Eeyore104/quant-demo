@echo off
rem SimNow 仿真常驻进程启动器（供 Windows 任务计划「quant-demo-live」调用；也可手动运行）
rem 说明：--real 为二次确认参数；是否真下单由 config.local.yaml 的 live.dry_run 决定
rem 停止：任务管理器中结束进程，或 schtasks /end /tn quant-demo-live
cd /d "%~dp0.."
echo [%date% %time%] 触发启动 >> "logs\live_trigger.log"
".venv\Scripts\python.exe" "run_live.py" --real >> "logs\live_task.log" 2>&1
