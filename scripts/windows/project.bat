@echo off
REM One-click launcher for AI Daily Work Record (web + scheduler, detached hidden processes).
REM Usage: project.bat start ^| restart ^| stop ^| status   (no args = interactive menu)
setlocal
cd /d "%~dp0..\.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0project.ps1" %*
set RC=%errorlevel%
if "%~1"=="" pause
endlocal & exit /b %RC%
