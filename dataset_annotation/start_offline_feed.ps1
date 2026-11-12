$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$datasetRoot = Join-Path $projectRoot 'data\datasets'
$python = Join-Path $projectRoot '.venv\Scripts\Open QQ Dot.exe'
$log = Join-Path $datasetRoot 'offline-feed.log'
$state = Join-Path $datasetRoot 'worker-state.json'
New-Item -ItemType Directory -Force -Path $datasetRoot | Out-Null
$Host.UI.RawUI.WindowTitle = 'MPDD + CPED offline annotation'
@{
    powershell_pid = $PID
    started_at = (Get-Date).ToString('o')
    project_root = $projectRoot
    log = $log
    database = Join-Path $datasetRoot 'annotations.sqlite3'
    state = 'starting'
} | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 $state
Set-Location $projectRoot
"Starting offline_feed at $(Get-Date -Format o); PowerShell PID=$PID" | Tee-Object -FilePath $log
$exitCode = 1
try {
    & $python -X utf8 -u (Join-Path $PSScriptRoot 'offline_feed.py') `
        --data-root 'E:\qwen35-nlp\data' `
        --db (Join-Path $datasetRoot 'annotations.sqlite3') `
        --batch-size 32 2>&1 | Tee-Object -FilePath $log -Append
    $exitCode = $LASTEXITCODE
} catch {
    $_ | Out-String | Tee-Object -FilePath $log -Append
    if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ne 0) { $exitCode = $LASTEXITCODE }
}
$existing = Get-Content $state -Raw | ConvertFrom-Json
$existing.state = if ($exitCode -eq 0) { 'stopped_or_complete' } else { 'failed' }
$existing | Add-Member -NotePropertyName exit_code -NotePropertyValue $exitCode -Force
$existing | Add-Member -NotePropertyName ended_at -NotePropertyValue (Get-Date).ToString('o') -Force
$existing | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 $state
"offline_feed exited with code $exitCode at $(Get-Date -Format o)" | Tee-Object -FilePath $log -Append
exit $exitCode
