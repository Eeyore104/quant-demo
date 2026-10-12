"""Windows 任务计划注册（v1.4）：每工作日定时运行每日流水线。

用法（项目根目录）：
    uv run python scripts/register_task.py --install              # 注册（工作日 17:00）
    uv run python scripts/register_task.py --install --time 16:30 # 指定时间
    uv run python scripts/register_task.py --status               # 查看状态
    uv run python scripts/register_task.py --run                  # 立即触发一次（验证）
    uv run python scripts/register_task.py --uninstall            # 卸载

说明：
- 通过 PowerShell ScheduledTasks 模块注册，含「错过补跑」（StartWhenAvailable）；
- 任务注册在周一至周五到点执行，法定节假日由 daily_run 的交易日判断自动跳过；
- 任务名：quant-demo-daily（实际执行 scripts/daily_run.bat）。
"""

import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK_NAME = "quant-demo-daily"


def run_ps(script: str) -> subprocess.CompletedProcess:
    """执行 PowerShell 脚本（写入临时 .ps1 再执行，规避命令行引号转义）。"""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".ps1", delete=False, encoding="utf-8-sig"
    ) as f:
        f.write(script)
        ps1 = f.name
    try:
        return subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1],
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        Path(ps1).unlink(missing_ok=True)


def print_result(proc: subprocess.CompletedProcess) -> int:
    if proc.stdout:
        print(proc.stdout.strip())
    if proc.stderr:
        print(proc.stderr.strip(), file=sys.stderr)
    return proc.returncode


def install(time_str: str) -> int:
    bat = PROJECT_ROOT / "scripts" / "daily_run.bat"
    if not bat.exists():
        print(f"未找到启动器：{bat}")
        return 1
    script = f"""
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c "{bat}"'
$trigger = New-ScheduledTaskTrigger -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At {time_str}
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $action `
    -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Output 'REGISTERED'
"""
    code = print_result(run_ps(script))
    if code == 0:
        print(f"已注册任务：{TASK_NAME}（工作日 {time_str}，错过自动补跑）")
        print("验证：uv run python scripts/register_task.py --run")
    return code


def uninstall() -> int:
    script = f"""
Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false
Write-Output 'REMOVED'
"""
    code = print_result(run_ps(script))
    if code == 0:
        print(f"已卸载任务：{TASK_NAME}")
    return code


def status() -> int:
    script = f"""
$t = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue
if ($null -eq $t) {{
    Write-Output 'NOT_FOUND'
}} else {{
    $info = Get-ScheduledTaskInfo -TaskName '{TASK_NAME}'
    Write-Output ('STATE=' + $t.State)
    Write-Output ('LAST_RUN=' + $info.LastRunTime)
    Write-Output ('NEXT_RUN=' + $info.NextRunTime)
    Write-Output ('LAST_RESULT=' + $info.LastTaskResult)
}}
"""
    return print_result(run_ps(script))


def trigger() -> int:
    script = f"Start-ScheduledTask -TaskName '{TASK_NAME}'\nWrite-Output 'TRIGGERED'\n"
    code = print_result(run_ps(script))
    if code == 0:
        print("已触发（结果稍后可用 --status 查看；日志见 logs/ 目录）")
    return code


def main() -> None:
    args = sys.argv[1:]
    if "--install" in args:
        time_str = "17:00"
        if "--time" in args:
            time_str = args[args.index("--time") + 1]
        sys.exit(install(time_str))
    if "--uninstall" in args:
        sys.exit(uninstall())
    if "--status" in args:
        sys.exit(status())
    if "--run" in args:
        sys.exit(trigger())
    print(__doc__)


if __name__ == "__main__":
    main()
