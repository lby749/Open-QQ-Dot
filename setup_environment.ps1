# 复用本机解释器和 uv 缓存；缺少 Python 时由启动器内置的 uv 安装。
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$bundledUv = Join-Path $PSScriptRoot '.bootstrap\uv.exe'
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
$uvPath = if (Test-Path -LiteralPath $bundledUv) { $bundledUv } elseif ($uvCommand) { $uvCommand.Source } else { $null }

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host '正在准备 Python 环境，优先使用本机已有解释器……'
    if ($uvPath) {
        $hostPython = & $uvPath python find --system --no-python-downloads '>=3.11,<3.14' 2>$null
        if ($LASTEXITCODE -eq 0 -and $hostPython) {
            & $uvPath venv --python ($hostPython | Select-Object -First 1) --seed .venv
        } else {
            Write-Host '未找到兼容解释器，将安装 Python 3.12；已有缓存会直接复用。'
            & $uvPath venv --python 3.12 --seed .venv
        }
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.13 -m venv .venv
        if ($LASTEXITCODE -ne 0) { & py -3 -m venv .venv }
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv .venv
    } else {
        throw '缺少启动器附带的 uv，请重新获取完整绿色包，或运行一键安装器。'
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $python)) {
        throw 'Python 环境创建失败，请检查上方下载信息。'
    }
}

Write-Host '正在检查并安装缺失的界面依赖，已有软件包与缓存不会重复下载……'
if ($uvPath) {
    & $uvPath pip install --python $python -r requirements-ui-core.txt
    if ($LASTEXITCODE -ne 0) { throw '基础依赖安装失败。' }
    & $uvPath pip install --python $python --no-deps -r requirements-ui-qfluent.txt
} else {
    & $python -X utf8 -m pip install --index-url https://pypi.org/simple --progress-bar off --timeout 120 -r requirements-ui.txt
}
if ($LASTEXITCODE -ne 0) { throw '界面依赖安装失败。' }

Write-Host '正在校验依赖是否真实可用……'
& $python -X utf8 -c "import PySide6, qfluentwidgets, PIL, psutil, websockets, jsonschema, numpy, pypdf, ijson, qrcode; print('imports ok')"
if ($LASTEXITCODE -ne 0) { throw '依赖导入校验失败。' }

Write-Host '正在检查可复用的 QQ 运行时……'
& $python -X utf8 dotlink_setup.py
if ($LASTEXITCODE -ne 0) {
    Write-Host '尚未找到 QQ 运行时，可在程序的 QQ 连接页自动安装。'
}

Write-Host ''
Write-Host '环境已准备好，双击 Open QQ Dot.exe 即可启动，也可运行：'
Write-Host '  .venv\Scripts\python.exe -X utf8 launcher.py'
Write-Host '本地模型按需安装；已下载的模型由所有好友共用。'
