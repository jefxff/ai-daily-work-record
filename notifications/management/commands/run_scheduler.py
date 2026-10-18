"""独立进程运行通知调度器与发送 worker。

用法：
    python manage.py run_scheduler            # 调度 + 发送，前台常驻
    python manage.py run_scheduler --no-worker  # 只跑定时任务

为什么需要它：多 worker 部署（gunicorn/uwsgi/waitress）下，进程内线程会给每个
worker 各起一份；而若按"是否 runserver"判断，非 runserver 部署会一份都不起，
定时任务静默停摆。把这件事交给显式命令，部署侧只需多一个 systemd 服务。
"""

import logging
import signal
import threading
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "运行通知队列调度器与发送线程（独立进程，勿在 web worker 内启动）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-worker",
            action="store_true",
            help="只跑定时任务，不启动站内/邮件发送线程（发送交给 notify_worker 命令时使用）",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="只执行一轮到期检查后退出（用于排障/测试）",
        )

    def handle(self, *args, **options):
        from notifications import scheduler

        if getattr(settings, "NOTIFICATIONS_SCHEDULER_INPROCESS", False):
            self.stdout.write(
                self.style.WARNING(
                    "NOTIFICATIONS_SCHEDULER_INPROCESS=True：web 进程内已有一份调度线程，"
                    "同时运行本命令会重复执行定时任务。请先在 settings 里关闭进程内调度。"
                )
            )

        if options["once"]:
            ran = scheduler.run_due_tasks()
            self.stdout.write(f"已执行 {ran} 个到期任务")
            return

        stop = threading.Event()

        def _stop(signum, frame):
            logger.info("run_scheduler 收到退出信号 %s", signum)
            stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _stop)
            except (ValueError, OSError):
                pass

        if not options["no_worker"]:
            worker = threading.Thread(
                target=_worker_loop, name="notify-worker", daemon=True
            )
            worker.start()

        logger.info("通知调度器已启动（独立进程）")
        self.stdout.write(self.style.SUCCESS("通知调度器已启动（Ctrl+C 退出）"))

        while not stop.is_set():
            scheduler.HEARTBEATS["scheduler"] = time.time()
            try:
                close_old_connections()
                scheduler.run_due_tasks(should_stop=stop.is_set)
            except Exception:
                logger.exception(
                    "通知定时任务调度轮询异常，%s 秒后重试", scheduler.SCAN_INTERVAL_SECONDS
                )
            stop.wait(scheduler.SCAN_INTERVAL_SECONDS)

        logger.info("通知调度器已退出")


def _worker_loop():
    """发送线程：与 notifications.apps._worker_loop 同构，供独立进程使用。"""
    from django.core.management import call_command

    from notifications import scheduler

    while True:
        scheduler.HEARTBEATS["worker"] = time.time()
        time.sleep(5)
        try:
            close_old_connections()
            call_command("notify_worker", "--once", stdout=_QuietOut())
        except Exception:
            logger.exception("notify_worker 后台线程处理异常")


class _QuietOut:
    def write(self, *args, **kwargs):
        pass

    def flush(self):
        pass

    def isatty(self):
        return False
