"""审计类数据的保留期清理（操作留痕 + 登录日志）。

两张表都只增不减（OperationLog 由全站留痕写入、LoginLog 由每次登录/登出/
注册申请写入），长期运行会持续膨胀。SQLite 是单文件数据库，删除行不自动
归还磁盘空间，因此本命令与 ``db_maintenance``（VACUUM）配合使用。

用法：
    python manage.py cleanup_operation_log --dry-run
    python manage.py cleanup_operation_log --days 365
    python manage.py cleanup_operation_log --days 365 --login-days 365

可在「系统 → 定时任务」中把 code 填为 cleanup_operation_log 并设置参数
days=365，由进程内/独立调度器每日执行（参数已加入 COMMAND_PARAMS 白名单）。
登录日志固定跟随同一个 ``--days``（另可用 ``--login-days`` 单独覆盖）。
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from records.models import OperationLog


class Command(BaseCommand):
    help = "删除超过保留期的操作留痕与登录日志（默认保留 730 天）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=getattr(settings, "OPERATION_LOG_RETENTION_DAYS", 730),
            help="保留天数，默认取 settings.OPERATION_LOG_RETENTION_DAYS（730）",
        )
        parser.add_argument(
            "--login-days",
            type=int,
            default=None,
            help="登录日志保留天数，缺省与 --days 相同",
        )
        parser.add_argument("--dry-run", action="store_true", help="只统计不删除")

    def handle(self, *args, **options):
        days = options["days"]
        if days <= 0:
            self.stderr.write("保留天数必须大于 0（不允许清空全部留痕）")
            return
        login_days = options.get("login_days") or days
        if login_days <= 0:
            self.stderr.write("登录日志保留天数必须大于 0")
            return

        cutoff = timezone.now() - timedelta(days=days)
        qs = OperationLog.objects.filter(operation_time__lt=cutoff)
        count = qs.count()

        from accounts.models import LoginLog

        login_cutoff = timezone.now() - timedelta(days=login_days)
        login_qs = LoginLog.objects.filter(created_at__lt=login_cutoff)
        login_count = login_qs.count()

        if options["dry_run"]:
            self.stdout.write(
                f"将删除 {count} 条 {cutoff:%Y-%m-%d} 之前的操作留痕、"
                f"{login_count} 条 {login_cutoff:%Y-%m-%d} 之前的登录日志（未执行）"
            )
            return

        deleted, _ = qs.delete()
        login_deleted, _ = login_qs.delete()
        self.stdout.write(self.style.SUCCESS(
            f"已删除 {deleted} 条 {cutoff:%Y-%m-%d} 之前的操作留痕（保留 {days} 天）、"
            f"{login_deleted} 条登录日志（保留 {login_days} 天）"
        ))
