@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Check RigPilot Controller Connection
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0portable-entry.ps1" -Action CheckConnection
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
    echo Controller connection checks passed.
) else (
    echo One or more controller connection checks failed. Exit code %EXIT_CODE%.
)
pause
exit /b %EXIT_CODE%
