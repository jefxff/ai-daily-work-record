"""保险续保预警扫描（阶段 3）：临期待续 + 应续未续。

用法（进程内调度器每日执行）：
    python manage.py check_insurance_renewal             # 默认提前 30 天
    python manage.py check_insurance_renewal --days 60

口径与 records.insurance_services.renewal_alerts 一致：以标的的最新保单
年度为准，同年度多条保单只提醒一次。

提醒节奏（按标的 + 保单年度去重，续保后新年度自动重新计算）：
- 临期待续：按 1/3/7/15/30/60/90/180/365 天里程碑各提醒一次，避免每日刷屏；
- 应续未续：断保 30 天内每周提醒一次，超过 30 天后每月提醒一次。

接收人：拥有 insurance.read 权限的启用用户（模块无归属人字段）。
"""
from urllib.parse import quote

from django.core.management.base import BaseCommand
from django.utils import timezone

from notifications import services
from records import insurance_services

MILESTONES = (1, 3, 7, 15, 30, 60, 90, 180, 365)


def _milestone(days_left, window):
    """把剩余天数映射到提醒里程碑：同一里程碑内只提醒一次。"""
    upper = max(window, days_left)
    for days in MILESTONES:
        if days_left <= days <= upper:
            return days
    return upper


def _lapsed_bucket(overdue_days):
    """断保提醒桶：前 30 天按周，之后按月。"""
    if overdue_days <= 30:
        return f"w{overdue_days // 7}"
    return f"m{overdue_days // 30}"


def _subject_link(subject):
    return f"/insurance/subjects/detail/?key={quote(insurance_services.subject_key(subject))}"


class Command(BaseCommand):
    help = "保险续保预警：临期待续 / 应续未续提醒（按标的与保单年度聚合）"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30,
                            help="提前提醒天数（默认 30）")

    def handle(self, *args, **opts):
        days = opts["days"]
        today = timezone.localdate()
        alerts = insurance_services.renewal_alerts(days=days, today=today)
        recipients = list(services.users_with_perm("insurance", "read"))
        if not recipients:
            self.stdout.write("临期待续: 新建 0 条")
            self.stdout.write("应续未续: 新建 0 条")
            return

        self.stdout.write(f"临期待续: 新建 {self._notify_due(alerts, recipients, days)} 条")
        self.stdout.write(f"应续未续: 新建 {self._notify_lapsed(alerts, recipients)} 条")

    def _notify_due(self, alerts, recipients, days):
        created = 0
        for item in alerts["due"]:
            subject, year = item["subject"], item["year"]
            context = {
                "title": (subject.label if subject.label == subject.insurance_type
                          else f"{subject.label} · {subject.insurance_type}"),
                "company_name": year.insurer or item["record"].company_name,
                "end_date": year.end_date,
                "days_left": item["days_left"],
            }
            dedup_context = (
                f"insurance_due:{subject.pk}:{year.pk}:"
                f"d{_milestone(item['days_left'], days)}"
            )
            for user in recipients:
                if services.has_emitted("insurance_expiry", user, dedup_context):
                    continue
                if services.emit(
                    "insurance_expiry",
                    recipient=user,
                    context=context,
                    dedup_context=dedup_context,
                    link=_subject_link(subject),
                ):
                    created += 1
        return created

    def _notify_lapsed(self, alerts, recipients):
        created = 0
        for item in alerts["lapsed"]:
            subject, year = item["subject"], item["year"]
            context = {
                "subject": subject.label,
                "insurance_type": subject.insurance_type,
                "company_name": year.insurer or item["record"].company_name,
                "end_date": year.end_date,
                "overdue_days": item["overdue_days"],
            }
            dedup_context = (
                f"insurance_lapsed:{subject.pk}:{year.pk}:"
                f"{_lapsed_bucket(item['overdue_days'])}"
            )
            for user in recipients:
                if services.has_emitted("insurance_lapsed", user, dedup_context):
                    continue
                if services.emit(
                    "insurance_lapsed",
                    recipient=user,
                    context=context,
                    dedup_context=dedup_context,
                    link=_subject_link(subject),
                ):
                    created += 1
        return created
