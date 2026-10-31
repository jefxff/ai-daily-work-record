@echo off
REM ==========================================================
REM HTTPS start script (intranet TLS)
REM   web   : scripts/serve_https.py (single process, threaded, stdlib TLS)
REM   worker: run_scheduler (separate process, same as HTTP mode)
REM Prereq:
REM   1) python scripts\gen_dev_cert.py                      (first run)
REM   2) certutil -addstore -f "ROOT" certs\ca.crt.pem       (as Administrator)
REM NOTE: messages are ASCII-only on purpose - cmd.exe reads .bat as ANSI(GBK)
REM       and UTF-8 Chinese text would be garbled on some consoles.
REM ==========================================================
cd /d "%~dp0..\.."

REM Resolve interpreter by Django version (single implementation: detect_python.ps1).
REM Using bare `python` from PATH breaks when the venv lives outside the project.
set "PYTHON="
for /f "delims=" %%i in ('powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0detect_python.ps1" -Print') do set "PYTHON=%%i"
if not defined PYTHON (
    echo [ERROR] No Python with Django found. Follow the hints printed above.
    pause
    exit /b 1
)
"%PYTHON%" --version
if %errorlevel% neq 0 (
    echo [ERROR] Python not runnable: %PYTHON%
    pause
    exit /b 1
)

if not exist certs\server.crt.pem (
    echo [INFO] certificate not found, generating...
    "%PYTHON%" scripts\gen_dev_cert.py
    if %errorlevel% neq 0 (
        echo [ERROR] cert generation failed
        pause
        exit /b 1
    )
    echo.
    echo [NEXT] Trust the local CA once, in an ADMIN PowerShell:
    echo        certutil -addstore -f "ROOT" certs\ca.crt.pem
    echo.
)

if not exist db.sqlite3 (
    echo [INFO] initializing database...
    "%PYTHON%" manage.py migrate
)

echo [INFO] collecting static files...
"%PYTHON%" manage.py collectstatic --noinput --clear

REM Production shape: DEBUG=0. ALLOWED_HOSTS auto-detects LAN IP; add hostname here.
set DJANGO_DEBUG=0
set DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
REM HTTPS_ONLY (secure cookies + __Host- prefix) is set inside serve_https.py
set HTTPS_PORT=8443

if not exist logs mkdir logs
start "WorkRecord-Scheduler" /min cmd /c ""%PYTHON%" manage.py run_scheduler >> logs\scheduler.out 2>&1"

echo ========================================
echo HTTPS : https://127.0.0.1:%HTTPS_PORT%/
echo Worker: started in background (logs\scheduler.out)
echo Ctrl+C stops web only; stop the worker in Task Manager.
echo ========================================
"%PYTHON%" scripts\serve_https.py --port %HTTPS_PORT%
pause
