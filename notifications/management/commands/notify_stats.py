"""通知模块监控指标输出与阈值告警检查。

用法：
    python manage.py notify_stats               # 输出关键指标
    python manage.py notify_stats --check       # 阈值检查：越限输出告警、退出码 1，
                                                # 并给超管发站内看门狗通知
指标：近 24h 发送成功率 / 平均与 P95 延迟 / 队列堆积 / 死信增量 / 重试分布。
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db.models import Count
from django.utils import timezone

from notifications import services
from notifications.models import Notification

# 阈值建议（可按实际负载调整）
THRESHOLDS = {
    "success_rate_warn": 0.95,      # 成功率 < 95% 警告
    "success_rate_crit": 0.85,      # 成功率 < 85% 严重
    "backlog_max": 100,             # 队列堆积 > 100 条
    "backlog_age_minutes": 10,      # 最老 pending > 10 分钟
    "dead_letter_daily": 5,         # 死信 > 5 条/日
}


class Command(BaseCommand):
    help = "通知模块监控指标与告警检查"

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="阈值检查模式")

    def handle(self, *args, **opts):
        now = timezone.now()
        since = now - timedelta(hours=24)

        total_done = Notification.objects.filter(
            created_at__gte=since,
            status__in=[Notification.Status.SENT, Notification.Status.FAILED],
        ).count()
        sent = Notification.objects.filter(
            created_at__gte=since, status=Notification.Status.SENT,
        ).count()
        failed_24h = Notification.objects.filter(
            created_at__gte=since, status=Notification.Status.FAILED,
        ).count()
        success_rate = (sent / total_done) if total_done else None

        latencies = list(
            Notification.objects.filter(
                status=Notification.Status.SENT, sent_at__isnull=False,
                created_at__gte=since,
            ).values_list("sent_at", "created_at")
        )
        delays = sorted((s - c).total_seconds() for s, c in latencies)
        avg_delay = sum(delays) / len(delays) if delays else None
        p95_delay = delays[int(len(delays) * 0.95)] if delays else None

        backlog = Notification.objects.filter(
            status__in=[Notification.Status.PENDING, Notification.Status.SENDING],
        ).count()
        oldest_pending = Notification.objects.filter(
            status=Notification.Status.PENDING,
        ).order_by("created_at").values_list("created_at", flat=True).first()
        backlog_age_min = (
            (now - oldest_pending).total_seconds() / 60 if oldest_pending else 0
        )

        retrying = Notification.objects.filter(
            status=Notification.Status.PENDING, retry_count__gt=0,
        ).aggregate(count=Count("id"), max_retry=Count("id"))  # 重试中的通知数
        retry_dist = list(Notification.objects.filter(retry_count__gt=0).values(
            "retry_count",
        ).annotate(n=Count("id")).order_by("retry_count"))

        lines = [
            "=== 通知模块监控指标（近 24h） ===",
            f"发送成功率: {'%.1f%%' % (success_rate * 100) if success_rate is not None else '无数据'}"
            f"（sent={sent}, failed={failed_24h}, total={total_done}）",
            f"平均发送延迟: {avg_delay:.1f}s" if avg_delay is not None else "平均发送延迟: 无数据",
            f"P95 发送延迟: {p95_delay:.1f}s" if p95_delay is not None else "P95 发送延迟: 无数据",
            f"队列堆积（pending+sending）: {backlog} 条，最老 {backlog_age_min:.1f} 分钟",
            f"重试中通知: {retrying['count']} 条；分布 {retry_dist}",
            f"死信（failed，近24h创建）: {failed_24h} 条",
        ]
        self.stdout.write("\n".join(lines))

        if not opts["check"]:
            return

        alerts = []
        if success_rate is not None:
            if success_rate < THRESHOLDS["success_rate_crit"]:
                alerts.append(f"严重：发送成功率 {success_rate:.1%} < "
                              f"{THRESHOLDS['success_rate_crit']:.0%}")
            elif success_rate < THRESHOLDS["success_rate_warn"]:
                alerts.append(f"警告：发送成功率 {success_rate:.1%} < "
                              f"{THRESHOLDS['success_rate_warn']:.0%}")
        if backlog > THRESHOLDS["backlog_max"]:
            alerts.append(f"警告：队列堆积 {backlog} > {THRESHOLDS['backlog_max']}")
        if backlog_age_min > THRESHOLDS["backlog_age_minutes"]:
            alerts.append(f"警告：最老 pending 已 {backlog_age_min:.0f} 分钟未消费")
        if failed_24h > THRESHOLDS["dead_letter_daily"]:
            alerts.append(f"警告：死信 {failed_24h} > {THRESHOLDS['dead_letter_daily']}/日")

        if alerts:
            self.stdout.write(self.style.ERROR("告警：\n- " + "\n- ".join(alerts)))
            User = get_user_model()
            count = services.announce(
                "通知模块告警",
                "检测到通知模块指标越限：\n" + "\n".join(f"- {a}" for a in alerts),
                created_by=None,
                recipients=User.objects.filter(is_superuser=True, is_active=True),
                priority="high",
            )
            self.stdout.write(f"看门狗通知已发给 {count} 位超管")
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("阈值检查通过"))
