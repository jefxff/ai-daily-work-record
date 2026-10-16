"""死信重投：将 failed 通知重置回 pending 队列。

用法：
    python manage.py notify_requeue --id 123   # 重投单条
    python manage.py notify_requeue --all-failed
"""
from django.core.management.base import BaseCommand, CommandError

from notifications import services
from notifications.models import Notification


class Command(BaseCommand):
    help = "死信（failed）通知手工重投"

    def add_arguments(self, parser):
        parser.add_argument("--id", type=int, help="要重投的通知 ID")
        parser.add_argument("--all-failed", action="store_true", help="重投全部死信")

    def handle(self, *args, **opts):
        qs = Notification.objects.filter(status=Notification.Status.FAILED)
        if opts["id"]:
            qs = qs.filter(pk=opts["id"])
        elif not opts["all_failed"]:
            raise CommandError("请指定 --id 或使用 --all-failed")
        count = 0
        for notification in qs:
            services.requeue(notification)
            count += 1
            self.stdout.write(f"已重投 notification#{notification.pk}")
        self.stdout.write(self.style.SUCCESS(f"共重投 {count} 条"))
