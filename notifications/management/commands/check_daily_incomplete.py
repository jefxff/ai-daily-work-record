"""每日记录定时扫描：当日未填写提醒 + 过期未完成记录提醒。

用法（Windows 计划任务 / cron 每日执行）：
    python manage.py check_daily_incomplete            # 扫描今日缺失 + 历史未完成
    python manage.py check_daily_incomplete --skip-history
幂等：dedup_context 含日期，同日重跑不会重复发送。
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from notifications import services
from records.models import DailyRecord


class Command(BaseCommand):
    help = "每日记录提醒扫描：当日未填写 / 过期未完成"

    def add_arguments(self, parser):
        parser.add_argument("--skip-history", action="store_true",
                            help="跳过过期未完成记录提醒，仅扫描当日缺失")

    def handle(self, *args, **opts):
        today = timezone.localdate()
        User = get_user_model()
        created = 0

        # ① 当日未填写提醒：启用用户中今日无任何每日记录者
        users_with_today = DailyRecord.objects.filter(
            record_date__date=today,
        ).values_list("user_id", flat=True)
        idle_users = User.objects.filter(is_active=True).exclude(
            id__in=users_with_today,
        )
        for user in idle_users:
            if services.emit(
                "daily_record_missing",
                recipient=user,
                context={"date": today},
                dedup_context=f"missing:{today}",
                link="/daily_record/add/",
            ):
                created += 1
        self.stdout.write(f"当日未填提醒：{created} 条（{today}）")

        # ② 过期未完成记录提醒：record_date 早于今天且仍为「未完成」
        if opts["skip_history"]:
            return
        history_created = 0
        overdue = DailyRecord.objects.filter(
            status="未完成", record_date__date__lt=today,
        ).select_related("user")
        for record in overdue:
            if services.emit(
                "daily_record_incomplete",
                recipient=record.user,
                context={
                    "record_date": timezone.localtime(record.record_date).date(),
                    "today": today,
                    "progress": record.progress,
                    "work_content": (record.work_content or "")[:50],
                },
                dedup_context=f"incomplete:{record.pk}:{today}",
                link=f"/daily_record/edit/{record.pk}/",
            ):
                history_created += 1
        self.stdout.write(
            f"过期未完成提醒：{history_created} 条（{overdue.count()} 条待提醒记录）"
        )
