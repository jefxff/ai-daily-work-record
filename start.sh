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

# 收集静态文件
echo -e "${YELLOW}收集静态文件...${NC}"
python manage.py collectstatic --noinput

# 迁移数据库（首次运行）
echo -e "${YELLOW}检查数据库...${NC}"
python manage.py migrate --run-syncdb

# 启动服务器
echo -e "${GREEN}启动 Django 服务...${NC}"
echo -e "访问地址: http://localhost:8000"
echo -e "API地址: http://localhost:8000/api"
echo -e ""
echo -e "按 Ctrl+C 停止服务"

python manage.py runserver 0.0.0.0:8000