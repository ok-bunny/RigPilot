@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Install RigPilot Agent Autostart
call "%~dp0Start-RigPilotAgent.bat" --install-autostart
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
    echo RigPilot Agent autostart is installed. Remote reboot and shutdown are enabled.
) else (
    echo Failed to install RigPilot Agent autostart. Exit code %EXIT_CODE%.
)
pause
exit /b %EXIT_CODE%
