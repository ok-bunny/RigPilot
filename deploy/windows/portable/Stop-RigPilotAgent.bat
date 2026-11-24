@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Stop RigPilot Agent Portable
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0portable-entry.ps1" -Action Stop
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
    echo Stop completed.
) else (
    echo Stop failed with exit code %EXIT_CODE%.
)
pause
exit /b %EXIT_CODE%
