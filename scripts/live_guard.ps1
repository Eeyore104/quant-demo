# v2.1 看门狗：只探「监听该端口的进程是不是 python」，是则退出；否则拉起常驻进程。
# 由 scripts\live_run.bat 调用（任务计划每 5 分钟触发）。
# 端口 49156 曾与 Clash Verge（verge-mihomo）冲突，导致探活误判 → 改用 49517，
# 且必须校验占用者进程名，避免任何其它程序占用时误以为实例存活（2026-10-26 实测）。
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

$port = 49517
$alive = $false
foreach ($c in (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) {
    $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
    if ($p -and $p.ProcessName -like 'python*') { $alive = $true }
}
if ($alive) { exit 0 }

$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { exit 1 }

# 启动用 ProcessStartInfo + UseShellExecute（不走 Start-Process）：
# PS 5.1 里 http_proxy / HTTP_PROXY 大小写并存时 Start-Process 会抛
# 「字典中的关键字已添加」而根本不启动子进程（2026-10-23 实测）。
try {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $py
    $psi.Arguments = 'run_live.py --real'
    $psi.WorkingDirectory = $root
    $psi.UseShellExecute = $true
    $psi.WindowStyle = 'Hidden'
    [System.Diagnostics.Process]::Start($psi) | Out-Null
} catch {
    $msg = "看门狗启动失败：$($_.Exception.Message)"
    Add-Content -Path (Join-Path $root 'logs\live_guard.err.log') -Value $msg
    exit 1
}
exit 0
