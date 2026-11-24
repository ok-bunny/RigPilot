@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Remove RigPilot Agent Autostart
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0portable-entry.ps1" -Action RemoveAutostart
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
    echo RigPilot Agent autostart is removed.
) else (
    echo Failed to remove RigPilot Agent autostart. Exit code %EXIT_CODE%.
)
pause
exit /b %EXIT_CODE%
