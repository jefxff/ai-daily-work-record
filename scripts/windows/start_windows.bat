@echo off
REM 切到项目根目录，保证相对路径（manage.py / logs / db.sqlite3）在双击运行时也正确
cd /d "%~dp0..\.."
echo ========================================
echo AI Daily Work Record - Windows启动脚本
echo ========================================

REM 解析 Python 解释器：按 requirements.txt 要求的 Django 版本挑（唯一实现见 detect_python.ps1），
REM 不再用 PATH 里那个裸 python——它往往没装 Django，启动会死在 No module named 'django'。
set "PYTHON="
for /f "delims=" %%i in ('powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0detect_python.ps1" -Print') do set "PYTHON=%%i"
if not defined PYTHON (
    echo [ERROR] 没有找到装了 Django 的 Python 解释器，请按上面的提示处理。
    pause
    exit /b 1
)
"%PYTHON%" --version

REM 检查数据库
if not exist db.sqlite3 (
    echo 初始化数据库...
    "%PYTHON%" manage.py migrate
)

REM 收集静态文件（审查 H4：DEBUG=0 下由 STATIC_ROOT 提供 /static/，必须先收集）
echo 收集静态文件...
"%PYTHON%" manage.py collectstatic --noinput --clear

REM 检查RBAC验收账号
echo 检查RBAC验收账号...
"%PYTHON%" manage.py shell -c "from django.contrib.auth.models import User; print('rbac_root 账号:', User.objects.filter(username='rbac_root').exists())"

REM 启动开发服务器
echo ========================================
echo 启动Django开发服务器...
echo 访问地址: http://127.0.0.1:8080/
echo ========================================
REM settings.py 默认 DEBUG=0（安全默认值）。启动脚本走生产模式，显式列出可访问主机。
REM _detect_allowed_hosts() 会自动探测当前机器的内网 IP 追加进来，DHCP 换 IP 无需改配置。
set DJANGO_DEBUG=0
set DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1

REM 独立调度进程：定时任务扫描 + 通知发送 worker（与 web 进程分离，DEBUG 开关不再影响调度）
if not exist logs mkdir logs
start "WorkRecord-Scheduler" /min cmd /c ""%PYTHON%" manage.py run_scheduler >> logs\scheduler.out 2>&1"

"%PYTHON%" manage.py runserver 0.0.0.0:8080 --noreload

pause
