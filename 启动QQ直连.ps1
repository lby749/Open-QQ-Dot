# 兼容入口：启动 DotLink QQ 直连运行时。
# 不再打开网页登录；扫码/快速登录在 Open QQ Dot 桌面的「QQ 直连」页完成。
param([switch]$Automatic)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw '请先运行 setup_environment.ps1，或使用一键安装器。'
}
& $python -X utf8 (Join-Path $projectRoot 'dotlink.py') start --home $projectRoot
if ($LASTEXITCODE -ne 0) { throw 'DotLink 未能启动，请查看 dotlink/runtime.out.log。' }
if (-not $Automatic) {
    & $python -X utf8 (Join-Path $projectRoot 'dotlink.py') status --home $projectRoot
}
