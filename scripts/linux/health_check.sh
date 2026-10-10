#!/bin/bash
# AI_daily_work_record 健康检查脚本
# 检查端口8001的Django服务是否运行，如未运行则重启

PORT=8001
APP_DIR="/home/jefxff/ai-daily-work-record-0525"
LOG_FILE="/home/jefxff/ai-daily-work-record-0525/scripts/linux/health_check.log"

# 检查端口是否在监听
if lsof -i:${PORT} >/dev/null 2>&1; then
    MSG="[$(date '+%Y-%m-%d %H:%M:%S')] ✅ AI_daily_work_record 运行正常 (端口${PORT})"
    echo "$MSG"
    echo "$MSG" >> $LOG_FILE
    exit 0
else
    MSG="[$(date '+%Y-%m-%d %H:%M:%S')] ⚠️ AI_daily_work_record 未运行，开始重启..."
    echo "$MSG"
    echo "$MSG" >> $LOG_FILE
    
    # 关闭可能存在的残留进程
    pkill -f "manage.py runserver.*${PORT}" 2>/dev/null
    sleep 2
    
    # 启动服务
    cd $APP_DIR
    nohup python3 manage.py runserver 0.0.0.0:${PORT} > /dev/null 2>&1 &
    
    sleep 3
    
    # 验证启动
    if lsof -i:${PORT} >/dev/null 2>&1; then
        MSG="[$(date '+%Y-%m-%d %H:%M:%S')] ✅ AI_daily_work_record 重启成功 (端口${PORT})"
        echo "$MSG"
        echo "$MSG" >> $LOG_FILE
        exit 0
    else
        MSG="[$(date '+%Y-%m-%d %H:%M:%S')] ❌ AI_daily_work_record 重启失败"
        echo "$MSG"
        echo "$MSG" >> $LOG_FILE
        exit 1
    fi
fi