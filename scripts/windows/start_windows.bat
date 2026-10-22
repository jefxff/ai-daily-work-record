@echo off
echo ========================================
echo AI Daily Work Record - Windows启动脚本
echo ========================================

REM 检查Python环境
python --version
if %errorlevel% neq 0 (
    echo 错误: Python未安装或不在PATH中
    pause
    exit /b 1
)

REM 检查依赖
pip list | findstr "django" > nul
if %errorlevel% neq 0 (
    echo 安装依赖包...
    pip install -r requirements.txt
)

REM 检查数据库
if not exist db.sqlite3 (
    echo 初始化数据库...
    python manage.py migrate
)

REM 检查RBAC验收账号
echo 检查RBAC验收账号...
python manage.py shell -c "from django.contrib.auth.models import User; print('rbac_root 账号:', User.objects.filter(username='rbac_root').exists())"

REM 启动开发服务器
echo ========================================
echo 启动Django开发服务器...
echo 访问地址: http://127.0.0.1:8080/
echo ========================================
REM settings.py 默认 DEBUG=0（安全默认值）。启动脚本走生产模式，显式列出可访问主机。
REM _detect_allowed_hosts() 会自动探测当前机器的内网 IP 追加进来，DHCP 换 IP 无需改配置。
set DJANGO_DEBUG=0
set DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
python manage.py runserver 0.0.0.0:8080

pause
