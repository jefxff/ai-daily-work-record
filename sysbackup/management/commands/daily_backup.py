"""每日自动备份：供进程内调度器（notifications.scheduler）按日调用。

不创建任何外部 Windows 计划任务 / cron，备份仍走备份中心的同一套
（SQLite 在线快照 + media + 加密 + 操作留痕），只是把「手动点一下」变成定时触发。
"""
from django.core.management.base import BaseCommand, CommandError

from sysbackup import services

DEFAULT_TIMEOUT = 1800  # 全量备份可能较慢，给 30 分钟


class Command(BaseCommand):
    help = "执行一次全量备份（定时任务调用入口）"

    def handle(self, *args, **options):
        try:
            task_id = services.start_backup(operator="定时任务")
        except RuntimeError as exc:
            raise CommandError(str(exc)) from exc
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        snap = services.wait_task(task_id, timeout=DEFAULT_TIMEOUT)
        if not snap:
            raise CommandError("备份任务状态丢失")
        if snap["status"] != "success":
            raise CommandError(
                f"备份失败：{snap.get('error') or snap.get('message') or snap['status']}"
            )
        result = snap.get("result") or {}
        self.stdout.write(self.style.SUCCESS(
            f"备份完成：{result.get('name') or result.get('path') or '见备份中心'}"
        ))
