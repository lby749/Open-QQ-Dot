# Stage the Tauri executable in a plain ASCII path.
# WebView2 returns "Access is denied" when the executable is launched from this
# repository's path (spaces, parentheses, and non-ASCII characters).
param(
    [string]$Destination = "$env:LOCALAPPDATA\OpenQQDot\Desktop"
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root 'desktop\src-tauri\target\release\open-qq-dot-desktop.exe'
if (-not (Test-Path -LiteralPath $exe)) {
    throw "Tauri executable not found: $exe`nRun: npm run tauri build -- --no-bundle"
}
New-Item -ItemType Directory -Force -Path $Destination | Out-Null
Copy-Item -LiteralPath $exe -Destination $Destination -Force
$legacy = Join-Path $root 'desktop\src-tauri\resources\legacy\Open QQ Dot.exe'
if (Test-Path -LiteralPath $legacy) {
    New-Item -ItemType Directory -Force -Path (Join-Path $Destination 'resources\legacy') | Out-Null
    Copy-Item -LiteralPath $legacy -Destination (Join-Path $Destination 'resources\legacy\Open QQ Dot.exe') -Force
}
$loader = Get-ChildItem "$env:USERPROFILE\.cargo\registry\src" -Recurse -Filter 'WebView2Loader.dll' -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match 'webview2-com-sys.*\\x64\\' } | Select-Object -First 1
if ($loader) {
    Copy-Item -LiteralPath $loader.FullName -Destination $Destination -Force
}
Start-Process -FilePath (Join-Path $Destination 'open-qq-dot-desktop.exe') -WorkingDirectory $Destination
Write-Host "Started $Destination\open-qq-dot-desktop.exe"
