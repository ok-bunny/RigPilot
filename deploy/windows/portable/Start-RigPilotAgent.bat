@echo off
setlocal EnableExtensions DisableDelayedExpansion
title RigPilot Agent Portable
if /I "%~1"=="--install-autostart" (
    "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0portable-entry.ps1" -Action InstallAutostart
) else (
    if not "%~1"=="" (
        echo Unsupported RigPilot Agent start option: %~1
        exit /b 2
    )
    "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0portable-entry.ps1" -Action Start
)
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo.
    echo RigPilot Agent stopped with exit code %EXIT_CODE%.
    if /I not "%~1"=="--install-autostart" (
        pause
    )
)
exit /b %EXIT_CODE%
