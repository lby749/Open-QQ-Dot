@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\build_windows_exe.ps1"
if errorlevel 1 (
  echo.
  echo EXE build failed. Check that Windows Python and PyInstaller are installed.
  pause
  exit /b 1
)
echo.
echo Build complete. The EXE must stay alongside the project files.
pause
