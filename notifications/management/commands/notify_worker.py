"""通知队列 worker：轮询 pending 通知，原子领取后逐渠道发送。

用法：
    python manage.py notify_worker              # 常驻循环（默认 5s 间隔）
    python manage.py notify_worker --once       # 处理一批后退出（适合计划任务/测试）
    python manage.py notify_worker --interval 10 --batch 50

失败处理：可重试错误按 60s × 2^n 指数退避，最多 5 次后转死信（failed）；
不可重试错误（如收件人无邮箱）直接死信。notify_requeue 可手工重投。
"""
import time

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from notifications import services
from notifications.channels import dispatch
from notifications.models import Notification, NotificationDelivery


class Command(BaseCommand):
    help = "通知队列 worker：领取 pending 通知并逐渠道发送"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="处理一批后退出")
        parser.add_argument("--interval", type=int, default=5, help="轮询间隔秒数")
        parser.add_argument("--batch", type=int, default=20, help="单批处理条数")

    def handle(self, *args, **opts):
        interval = max(1, opts["interval"])
        batch = max(1, opts["batch"])
        self.stdout.write(self.style.SUCCESS(
            f"notify_worker 启动（interval={interval}s, batch={batch}）"
        ))
        while True:
            processed = self.process_batch(batch)
            if opts["once"]:
                break
            if not processed:
                time.sleep(interval)

    def process_batch(self, batch):
        now = timezone.now()
        due = Notification.objects.filter(
            status=Notification.Status.PENDING,
            scheduled_at__lte=now,
        ).filter(
            Q(next_retry_at__isnull=True) | Q(next_retry_at__lte=now)
        ).order_by("created_at").values_list("pk", flat=True)[:batch]

        processed = 0
        for pk in due:
            if services.expire_if_overdue(pk):
                processed += 1
                continue
            # 原子领取：仅抢到 update 的 worker 发送，防并发双发
            if not services.claim_for_send(pk):
                continue
            notification = Notification.objects.select_related(
                "template", "recipient",
            ).get(pk=pk)
            self.send_one(notification)
            processed += 1
        return processed

    def send_one(self, notification):
        results = dispatch(notification)
        effective = [r for r in results if r.status != NotificationDelivery.ChannelStatus.SKIPPED]
        self.stdout.write(
            f"notification#{notification.pk} [{notification.template.code}] → "
            + ", ".join(f"{r.channel}:{r.status}" for r in results)
        )
        if not effective:
            # 全部渠道被跳过（如仅邮件模板但用户关闭邮件）：视为发送完成
            services.mark_sent(notification)
            return
        if all(r.ok for r in effective):
            services.mark_sent(notification)
            return
        # 任一渠道出现不可重试错误即死信：重试无法修复（如收件人无邮箱），
        # 尽早转入 failed 供 notify_stats 监控与 notify_requeue 人工处理
        permanent_fail = any(r.permanent for r in effective)
        error_detail = "; ".join(
            f"{r.channel}: {r.detail}" for r in effective if not r.ok
        )
        if permanent_fail:
            services.mark_failed(notification, error_detail)
        else:
            services.mark_retry(notification, error_detail)
