param(
    [ValidateSet('setup','start','stop','status','run','bind')]
    [string]$Action = 'status',
    [string]$QQ
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root '.venv\Scripts\Open QQ Dot.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = Join-Path $root '.venv\Scripts\python.exe' }
$main = Join-Path $root 'main.py'
$pidFile = Join-Path $root 'bridge.pid'

function Get-BridgeProcess {
    if (-not (Test-Path -LiteralPath $pidFile)) { return $null }
    $savedPid = 0
    if (-not [int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$savedPid)) { return $null }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $savedPid" -ErrorAction SilentlyContinue
    if ($proc -and $proc.CommandLine -and $proc.CommandLine.Contains($main)) { return $proc }
    return $null
}

switch ($Action) {
    'setup' {
        if (-not (Test-Path -LiteralPath $python)) {
            & python -m venv (Join-Path $root '.venv')
            if ($LASTEXITCODE -ne 0) { throw 'Failed to create dedicated Python virtual environment' }
        }
        & $python -m pip install -r (Join-Path $root 'requirements.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
        Write-Output 'Dependencies installed. Auto-start is not configured.'
    }
    'start' {
        $running = Get-BridgeProcess
        if ($running) { Write-Output "Running, PID $($running.ProcessId)"; break }
        if (-not (Test-Path -LiteralPath $python)) { throw 'Please run .\bridge.ps1 setup first' }
        $proc = Start-Process -FilePath $python -ArgumentList @('-X','utf8','-u',"`"$main`"",'run') -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $root 'bridge.log') -RedirectStandardError (Join-Path $root 'bridge.err.log') -PassThru
        Set-Content -LiteralPath $pidFile -Value $proc.Id
        Start-Sleep -Seconds 2
        if (-not (Get-BridgeProcess)) { throw 'Startup failed, check bridge.err.log' }
        Write-Output "Running, PID $($proc.Id)"
    }
    'stop' {
        $running = Get-BridgeProcess
        if (-not $running) { Write-Output 'Not running'; break }
        Stop-Process -Id $running.ProcessId
        Wait-Process -Id $running.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
        if (Get-BridgeProcess) { throw 'Bridge failed to stop' }
        Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
        Write-Output 'Bridge stopped'
    }
    'status' {
        $running = Get-BridgeProcess
        if ($running) { Write-Output "Running, PID $($running.ProcessId)" } else { Write-Output 'Not running' }
        if (Test-Path -LiteralPath $python) { & $python -X utf8 $main status }
        $log = Join-Path $root 'bridge.log'
        if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log -Tail 8 -Encoding UTF8 }
    }
    'run' {
        if (-not (Test-Path -LiteralPath $python)) { throw 'Please run .\bridge.ps1 setup first' }
        & $python -X utf8 -u $main run
    }
    'bind' {
        if ($QQ -notmatch '^\d{5,12}$') { throw 'Please specify 5-12 digit QQ ID with -QQ' }
        if (-not (Test-Path -LiteralPath $python)) { throw 'Please run .\bridge.ps1 setup first' }
        & $python -X utf8 $main bind --qq-id $QQ
        if ($LASTEXITCODE -ne 0) { throw 'Friend binding failed' }
    }
}
