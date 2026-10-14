"""Windows 任务计划注册：每日流水线（v1.4）与仿真守护（v2.0）。

用法（项目根目录）：
    uv run python scripts/register_task.py --install              # 每日流水线（工作日 17:00）
    uv run python scripts/register_task.py --install --time 16:30 # 指定时间
    uv run python scripts/register_task.py --status               # 查看状态
    uv run python scripts/register_task.py --run                  # 立即触发一次（验证）
    uv run python scripts/register_task.py --uninstall            # 卸载
    uv run python scripts/register_task.py --install --live       # 仿真守护（开机自启 + 失败重启）
    uv run python scripts/register_task.py --status --live        # 仿真守护状态

说明：
- 通过 PowerShell ScheduledTasks 模块注册；
- 每日任务（quant-demo-daily）：工作日到点执行 + 错过补跑（节假日由 daily_run 判断）；
- 仿真守护（quant-demo-live）：开机启动（延迟 2 分钟）常驻运行 run_live.py，
  任务失败自动重启（最多 10 次 × 5 分钟间隔），**执行时限设为无限制**（常驻进程），
  电池模式不停止（笔记本场景）；是否真实下单由 config.local.yaml 的 dry_run 控制。
"""

import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK_NAME = "quant-demo-daily"
LIVE_TASK_NAME = "quant-demo-live"


def run_ps(script: str) -> subprocess.CompletedProcess:
    """执行 PowerShell 脚本（写入临时 .ps1 再执行，规避命令行引号转义）。

    编码：中文 Windows 的 PowerShell 输出为 GBK；``errors="replace"`` 兜底
    （个别字节无法解码时替换而不抛异常，不影响退出码判断）。
    """
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
            encoding="gbk",
            errors="replace",
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
$ErrorActionPreference = 'Stop'
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


def install_live() -> int:
    """仿真守护任务：登录自启（延迟 2 分钟）+ 失败重启 + 无执行时限（常驻）。

    触发器为 ``AtLogOn``（用户级任务，普通权限即可注册）；「开机自启（未登录也运行）」
    需管理员权限与 ``AtStartup`` 触发器，不在默认流程内。
    """
    bat = PROJECT_ROOT / "scripts" / "live_run.bat"
    if not bat.exists():
        print(f"未找到启动器：{bat}")
        return 1
    script = f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c "{bat}"'
$t1 = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$t1.Delay = 'PT2M'
# 持续自愈：注册后立即生效、每 5 分钟触发一次（实例在跑时由 IgnoreNew 忽略；失联则快速捞回）
$t2 = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName '{LIVE_TASK_NAME}' -Action $action `
    -Trigger @($t1, $t2) -Settings $settings -Force | Out-Null
Write-Output 'REGISTERED'
"""
    code = print_result(run_ps(script))
    if code == 0:
        print(f"已注册任务：{LIVE_TASK_NAME}（登录自启 + 每 5 分钟自愈 + 失败重启 + 执行无时限）")
        print("提示：真实下单需 config.local.yaml 设 live.dry_run=false（启动器已带 --real）")
        print("提示：保持本机常开且处于登录状态即可满足仿真运行前提")
    return code


def uninstall(task: str = TASK_NAME) -> int:
    script = f"""
$ErrorActionPreference = 'Stop'
Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false
Write-Output 'REMOVED'
"""
    code = print_result(run_ps(script))
    if code == 0:
        print(f"已卸载任务：{task}")
    return code


def status(task: str = TASK_NAME) -> int:
    script = f"""
$t = Get-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue
if ($null -eq $t) {{
    Write-Output 'NOT_FOUND'
}} else {{
    $info = Get-ScheduledTaskInfo -TaskName '{task}'
    Write-Output ('STATE=' + $t.State)
    Write-Output ('LAST_RUN=' + $info.LastRunTime)
    Write-Output ('NEXT_RUN=' + $info.NextRunTime)
    Write-Output ('LAST_RESULT=' + $info.LastTaskResult)
}}
"""
    return print_result(run_ps(script))


def trigger(task: str = TASK_NAME) -> int:
    script = (
        f"$ErrorActionPreference = 'Stop'\n"
        f"Start-ScheduledTask -TaskName '{task}'\n"
        "Write-Output 'TRIGGERED'\n"
    )
    code = print_result(run_ps(script))
    if code == 0:
        print("已触发（结果稍后可用 --status 查看；日志见 logs/ 目录）")
    return code


def main() -> None:
    args = sys.argv[1:]
    use_live = "--live" in args
    task = LIVE_TASK_NAME if use_live else TASK_NAME
    if "--install" in args:
        if use_live:
            sys.exit(install_live())
        time_str = "17:00"
        if "--time" in args:
            time_str = args[args.index("--time") + 1]
        sys.exit(install(time_str))
    if "--uninstall" in args:
        sys.exit(uninstall(task))
    if "--status" in args:
        sys.exit(status(task))
    if "--run" in args:
        sys.exit(trigger(task))
    print(__doc__)


if __name__ == "__main__":
    main()
