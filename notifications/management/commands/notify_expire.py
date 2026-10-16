"""通知过期兜底清理：pending 且已过 expires_at → expired；可选清理历史已读数据。

用法：
    python manage.py notify_expire                    # 仅过期兜底
    python manage.py notify_expire --purge-days 90   # 额外删除 90 天前已读/撤回/过期的通知
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from notifications import services
from notifications.models import Notification


class Command(BaseCommand):
    help = "通知过期兜底与历史数据清理"

    def add_arguments(self, parser):
        parser.add_argument(
            "--purge-days", type=int, default=0,
            help="删除 N 天前已读/已撤回/已过期的通知（0=不清理）",
        )

    def handle(self, *args, **opts):
        expired = services.expire_overdue()
        self.stdout.write(f"过期兜底：{expired} 条 pending 已置为 expired")
        days = opts["purge_days"]
        if days > 0:
            threshold = timezone.now() - timedelta(days=days)
            deleted, _ = Notification.objects.filter(
                status__in=[
                    Notification.Status.REVOKED, Notification.Status.EXPIRED,
                ],
                updated_at__lt=threshold,
            ).delete()
            deleted += Notification.objects.filter(
                status=Notification.Status.SENT,
                is_read=True,
                read_at__lt=threshold,
            ).delete()[0]
            self.stdout.write(f"历史清理：删除 {deleted} 条 {days} 天前的已读/作废通知")
