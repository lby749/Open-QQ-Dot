$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$log = Join-Path $projectRoot 'data\datasets\bge-m3-clean-index.log'
$python = Join-Path $projectRoot '.venv\Scripts\Open QQ Dot.exe'
Set-Location $projectRoot
"Starting BGE-M3 clean index at $(Get-Date -Format o)" | Tee-Object -FilePath $log
& $python -B -X utf8 -u (Join-Path $PSScriptRoot 'embed_clean.py') 2>&1 | Tee-Object -FilePath $log -Append
$code = $LASTEXITCODE
"BGE-M3 clean index exited with code $code at $(Get-Date -Format o)" | Tee-Object -FilePath $log -Append
if ($code -ne 0) { throw "BGE-M3 clean index failed with exit code $code" }
