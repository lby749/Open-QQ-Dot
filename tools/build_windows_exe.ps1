# Build the Windows launcher EXE *on Windows*. The project resources and Python
# source still need to be distributed alongside the executable: this is not an
# independently self-contained executable.
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $root
try {
    if ($env:OS -ne 'Windows_NT') { throw 'The Open QQ Dot Windows EXE must be built on Windows.' }
    $python = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) { $python = 'python' }
    & $python -c 'import PyInstaller'
    if ($LASTEXITCODE -ne 0) { throw 'Install PyInstaller in the current environment first.' }
    & $python -m PyInstaller --noconfirm --clean '.\Open QQ Dot.spec'
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
    $built = Join-Path $root 'dist\Open QQ Dot.exe'
    if (-not (Test-Path $built)) { throw 'Expected EXE output is missing.' }
    Copy-Item -LiteralPath $built -Destination (Join-Path $root 'Open QQ Dot.exe') -Force
    Write-Host 'Built Open QQ Dot.exe. Keep it alongside all project source, assets and installer scripts.'
    Write-Host 'EXE build does not imply that NapCat, Qt rendering or online API connections have passed functional tests.'
} finally { Pop-Location }
