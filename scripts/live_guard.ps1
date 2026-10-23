# v2.1 看门狗：端口轻探，存活即退出（不启 python）；仅真死时拉起常驻进程。
# 由 scripts\live_run.bat 调用（任务计划每 5 分钟触发）。
# 启动方式用 ProcessStartInfo + UseShellExecute（不走 Start-Process）：
# PS 5.1 里 http_proxy / HTTP_PROXY 大小写并存时 Start-Process 会抛
# 「字典中的关键字已添加」而根本不启动子进程（2026-10-23 实测）。
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

$port = 49156
$alive = $false
try {
    $client = New-Object System.Net.Sockets.TcpClient
    $iar = $client.BeginConnect('127.0.0.1', $port, $null, $null)
    if ($iar.AsyncWaitHandle.WaitOne(300)) {
        $client.EndConnect($iar)
        $alive = $true
    }
    $client.Close()
} catch {
    $alive = $false
}

if ($alive) { exit 0 }

$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { exit 1 }

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
