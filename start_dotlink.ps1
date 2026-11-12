# Compatibility entry: start the DotLink QQ runtime.
# The old script opened a WebUI login page; DotLink embeds quick login and the
# QR flow in the desktop app, so this only starts/repairs the runtime.
param([switch]$Automatic)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Run setup_environment.ps1 first (or use the one-click installer).'
}
& $python -X utf8 (Join-Path $projectRoot 'dotlink.py') start --home $projectRoot
if ($LASTEXITCODE -ne 0) { throw 'DotLink did not start; see dotlink/runtime.out.log.' }
if (-not $Automatic) {
    & $python -X utf8 (Join-Path $projectRoot 'dotlink.py') status --home $projectRoot
}
