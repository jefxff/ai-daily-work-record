"""notifications 应用配置。

ready() 在 runserver 长驻进程内启动两个后台线程：
1. 发送线程（daemon worker）：站内/邮件队列无需人工启动 manage.py notify_worker，
   进程存活即自动消费。manage.py notify_worker 命令仍保留，供计划任务或独立部署使用；
   两者基于原子领取（claim_for_send）可安全并存，不会重复发送。
2. 调度线程（scheduler.scheduler_loop）：按 ScheduledTask 配置执行到期提醒扫描、
   每日记录提醒、过期兜底、健康巡检等定时任务，可在通知中心页面由超管配置。
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

    from . import scheduler

    while True:
        scheduler.HEARTBEATS["worker"] = time.time()
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
        from . import scheduler

        thread = threading.Thread(target=_worker_loop, name="notify-worker", daemon=True)
        thread.start()
        # 进程内定时任务调度：到期提醒/每日记录提醒/过期兜底/健康巡检
        sched_thread = threading.Thread(target=scheduler.scheduler_loop, name="notify-scheduler", daemon=True)
        sched_thread.start()
