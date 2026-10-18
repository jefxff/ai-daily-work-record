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
        # 是否在进程内起线程由配置决定（settings.NOTIFICATIONS_SCHEDULER_INPROCESS）。
        # 改造前用 `"runserver" in sys.argv` 判断运行环境：换成 gunicorn/uwsgi 后
        # 定时任务会全部静默停摆且不报错。现在改为显式开关：
        #   True  —— 开发/单进程 runserver，随进程启动（保持原行为）
        #   False —— 多 worker 部署，由 `manage.py run_scheduler` 独立进程承担
        from django.conf import settings

        if not getattr(settings, "NOTIFICATIONS_SCHEDULER_INPROCESS", False):
            return

        # 只有 runserver 才需要在进程内起线程。manage.py 的短命令（test / migrate /
        # shell / run_scheduler …）以及 `python script.py`、一次性的管理脚本都不该起：
        # 后台线程会在这些进程里抢写数据库，产生
        # "attempt to write a readonly database" 之类的噪音错误，也会污染测试数据。
        # 若确实要在 gunicorn/uwsgi 等非 runserver 容器里进程内调度，
        # 显式设 DJANGO_SCHEDULER_FORCE_INPROCESS=1。
        is_runserver = "runserver" in sys.argv
        if not is_runserver and os.environ.get("DJANGO_SCHEDULER_FORCE_INPROCESS") != "1":
            return
        # runserver 的重载器有两个进程，只在真正干活的子进程里起
        if is_runserver and not ("--noreload" in sys.argv or os.environ.get("RUN_MAIN") == "true"):
            return
        from . import scheduler

        thread = threading.Thread(target=_worker_loop, name="notify-worker", daemon=True)
        thread.start()
        # 进程内定时任务调度：到期提醒/每日记录提醒/过期兜底/健康巡检
        sched_thread = threading.Thread(target=scheduler.scheduler_loop, name="notify-scheduler", daemon=True)
        sched_thread.start()
