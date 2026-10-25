"""SQLite 维护：WAL 检查点 + VACUUM + ANALYZE。

为什么单机 SQLite 部署需要它（审查 H6 / W6）：
- 删除行（留痕/登录日志清理）不会把空间还给文件系统，库文件只增不减；
- 表统计信息陈旧会让查询计划退化（本项目表多、过滤字段多）；
- WAL 文件长期只增不减会拖慢每次读。

调度建议：每日跑一次，命令内部按 ``--min-interval-days``（默认 28 天）节流，
避免每天对大库做 VACUUM（VACUUM 会重写整个文件，需要同等大小的空闲空间）。

用法：
    python manage.py db_maintenance --dry-run
    python manage.py db_maintenance --force
"""

import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone

STATE_KEY = "db_maintenance_last_run"


class Command(BaseCommand):
    help = "SQLite 维护：wal_checkpoint(TRUNCATE) + VACUUM + ANALYZE（默认 28 天节流）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--min-interval-days",
            type=int,
            default=28,
            help="距上次执行不足该天数则跳过（默认 28）",
        )
        parser.add_argument("--force", action="store_true", help="忽略节流，强制执行")
        parser.add_argument("--dry-run", action="store_true", help="只打印将执行的动作")

    def handle(self, *args, **options):
        from django.core.cache import cache

        if connection.vendor != "sqlite":
            self.stdout.write("非 SQLite 后端，跳过（本命令只服务单机 SQLite 部署）")
            return

        min_days = max(1, int(options["min_interval_days"]))
        last = cache.get(STATE_KEY)
        now = timezone.now()
        if last and not options["force"]:
            elapsed = (now - last).days
            if elapsed < min_days:
                self.stdout.write(f"上次维护在 {elapsed} 天前（< {min_days} 天），本次跳过")
                return

        db_size = self._db_size()
        if options["dry_run"]:
            self.stdout.write(
                f"[dry-run] 将执行 wal_checkpoint(TRUNCATE) / VACUUM / ANALYZE，"
                f"当前库文件 {db_size:.1f} MB"
            )
            return

        started = time.monotonic()
        with connection.cursor() as cur:
            cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # VACUUM 必须不在事务中执行：Django 默认 autocommit，此处安全
        connection.cursor().execute("VACUUM")
        connection.cursor().execute("ANALYZE")
        cache.set(STATE_KEY, now, timeout=60 * 60 * 24 * 365)
        self.stdout.write(self.style.SUCCESS(
            f"数据库维护完成：{db_size:.1f} MB → {self._db_size():.1f} MB，"
            f"耗时 {time.monotonic() - started:.1f}s"
        ))

    @staticmethod
    def _db_size() -> float:
        """当前数据库主文件大小（MB）。"""
        import os

        path = str(settings.DATABASES["default"]["NAME"])
        try:
            return os.path.getsize(path) / 1024 / 1024
        except OSError:
            return 0.0
