#!/bin/bash
# HTTPS 启动脚本（内网 TLS 版）
#   web   : scripts/serve_https.py（单进程多线程 + 标准库 TLS）
#   worker: run_scheduler（独立进程，与 HTTP 版一致）
# 前置：
#   1) python scripts/gen_dev_cert.py            首次生成证书
#   2) 把 certs/ca.crt.pem 装进系统/浏览器受信任根（各平台方式不同）
set -e

cd "$(dirname "$0")"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

if [ ! -f certs/server.crt.pem ]; then
  echo -e "${YELLOW}未找到证书，正在生成...${NC}"
  python3 scripts/gen_dev_cert.py
  echo -e "${YELLOW}请把 certs/ca.crt.pem 安装为受信任根证书，否则浏览器会告警${NC}"
fi

echo -e "${YELLOW}收集静态文件...${NC}"
python3 manage.py collectstatic --noinput --clear

export DJANGO_DEBUG=0
export DJANGO_ALLOWED_HOSTS="localhost,127.0.0.1"
export HTTPS_PORT="${HTTPS_PORT:-8443}"

mkdir -p logs
python3 manage.py run_scheduler >> logs/scheduler.out 2>&1 &
SCHEDULER_PID=$!
trap 'kill "$SCHEDULER_PID" 2>/dev/null' EXIT INT TERM

echo -e "${GREEN}HTTPS 服务: https://127.0.0.1:${HTTPS_PORT}/${NC}"
python3 scripts/serve_https.py --port "$HTTPS_PORT"
