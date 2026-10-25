#!/bin/bash

# AI Daily Work Record - 启动脚本
# 使用方法: ./start.sh

# 设置颜色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== AI Daily Work Record 启动脚本 ===${NC}"

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo -e "${YELLOW}安装 Python...${NC}"
    apt update && apt install -y python3 python3-pip
fi

# 安装依赖
echo -e "${YELLOW}安装依赖...${NC}"
pip install -r requirements.txt

# 收集静态文件（审查 H4：DEBUG=0 下由 STATIC_ROOT 提供 /static/，必须先收集）
echo -e "${YELLOW}收集静态文件...${NC}"
python manage.py collectstatic --noinput --clear

# 迁移数据库（首次运行）
echo -e "${YELLOW}检查数据库...${NC}"
python manage.py migrate --run-syncdb

# 启动服务器
echo -e "${GREEN}启动 Django 服务...${NC}"
echo -e "访问地址: http://localhost:8000"
echo -e "API地址: http://localhost:8000/api"
echo -e ""
echo -e "按 Ctrl+C 停止服务"

# settings.py 默认 DEBUG=0（安全默认值）。启动脚本走生产模式，显式列出可访问主机。
# _detect_allowed_hosts() 会自动探测当前机器的内网 IP 追加进来，DHCP 换 IP 无需改配置。
export DJANGO_DEBUG=0
export DJANGO_ALLOWED_HOSTS="localhost,127.0.0.1"

# 独立调度进程：定时任务扫描 + 通知发送 worker。
# 调度已与 web 进程解耦，DJANGO_DEBUG 不再影响定时任务是否执行。
mkdir -p logs
python manage.py run_scheduler >> logs/scheduler.out 2>&1 &
SCHEDULER_PID=$!
trap 'kill "$SCHEDULER_PID" 2>/dev/null' EXIT INT TERM

python manage.py runserver 0.0.0.0:8000 --noreload