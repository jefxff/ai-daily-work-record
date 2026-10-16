"""notifications 应用配置。

ready() 在 runserver 长驻进程内启动后台发送线程（daemon worker）：
站内/邮件队列无需人工启动 manage.py notify_worker，进程存活即自动消费。
manage.py notify_worker 命令仍保留，供计划任务或独立部署使用；
两者基于原子领取（claim_for_send）可安全并存，不会重复发送。
"""
import logging
import os
import sys
import threading
import time

from django.apps import AppConfig
from django.db import close_old_connections

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5


class _QuietOut:
    """吞掉 call_command 的 stdout 输出，避免后台线程刷屏 runserver 控制台。"""

    def write(self, *args, **kwargs):
        pass

    def flush(self):
        pass

    def isatty(self):
        return False


def _worker_loop():
    """后台发送循环：每轮处理一批 pending 通知后小睡。"""
    from django.core.management import call_command

    while True:
        time.sleep(POLL_INTERVAL_SECONDS)
        try:
            close_old_connections()
            call_command("notify_worker", "--once", stdout=_QuietOut())
        except Exception:  # 后台线程兜底：单轮失败不影响下一轮
            logger.exception("notify_worker 后台线程处理异常")


class NotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "notifications"
    verbose_name = "消息通知"

    def ready(self):
        # 仅长驻 web 进程启动：runserver --noreload 主进程，或 autoreload 子进程（RUN_MAIN=true）
        if "runserver" not in sys.argv:
            return
        if not ("--noreload" in sys.argv or os.environ.get("RUN_MAIN") == "true"):
            return
        thread = threading.Thread(target=_worker_loop, name="notify-worker", daemon=True)
        thread.start()
