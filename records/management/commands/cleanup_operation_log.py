"""操作留痕保留期清理（复核报告 DATA-03）。

操作留痕表只增不减，长期运行会持续膨胀。本命令按保留天数删除过期记录，
默认保留 730 天，可通过 settings.OPERATION_LOG_RETENTION_DAYS 或 --days 覆盖。

用法：
    python manage.py cleanup_operation_log --dry-run
    python manage.py cleanup_operation_log --days 365

可在「系统 → 定时任务」中把 code 填为 cleanup_operation_log 并设置参数
days=365，由进程内/独立调度器每日执行（参数已加入 COMMAND_PARAMS 白名单）。
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from records.models import OperationLog


class Command(BaseCommand):
    help = "删除超过保留期的操作留痕（默认保留 730 天）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=getattr(settings, "OPERATION_LOG_RETENTION_DAYS", 730),
            help="保留天数，默认取 settings.OPERATION_LOG_RETENTION_DAYS（730）",
        )
        parser.add_argument("--dry-run", action="store_true", help="只统计不删除")

    def handle(self, *args, **options):
        days = options["days"]
        if days <= 0:
            self.stderr.write("保留天数必须大于 0（不允许清空全部留痕）")
            return
        cutoff = timezone.now() - timedelta(days=days)
        qs = OperationLog.objects.filter(operation_time__lt=cutoff)
        count = qs.count()
        if options["dry_run"]:
            self.stdout.write(f"将删除 {count} 条 {cutoff:%Y-%m-%d} 之前的操作留痕（未执行）")
            return
        deleted, _ = qs.delete()
        self.stdout.write(self.style.SUCCESS(
            f"已删除 {deleted} 条 {cutoff:%Y-%m-%d} 之前的操作留痕（保留 {days} 天）"
        ))
