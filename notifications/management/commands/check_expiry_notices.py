"""到期提醒扫描：证照 / 档案合同 / 借阅归还。

保险到期/续保预警已独立为 check_insurance_renewal（按标的与保单年度聚合，
避免同一标的同年度多条保单重复提醒）。

用法（进程内调度器 / cron 每日执行）：
    python manage.py check_expiry_notices              # 默认提前 7 天
    python manage.py check_expiry_notices --days 14
幂等：dedup_context 含对象主键与日期，同日重跑不会重复发送。

通知对象（模块无归属用户字段，按 RBAC 权限定位接收人）：
- 证照/借阅/档案合同：拥有 archive.read 权限的用户
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from notifications import services


class Command(BaseCommand):
    help = "到期提醒扫描：证照 / 档案合同 / 借阅归还"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7,
                            help="提前提醒天数（默认 7）")

    def handle(self, *args, **opts):
        today = timezone.localdate()
        deadline = today + timedelta(days=opts["days"])
        stats = []

        stats.append(self._licenses(today, deadline))
        stats.append(self._archive_contracts(today, deadline))
        stats.append(self._borrows(today))

        for label, created in stats:
            self.stdout.write(f"{label}: 新建 {created} 条")

    # ---- 各场景 ----

    def _licenses(self, today, deadline):
        from archive.models import License

        created = 0
        recipients = list(services.users_with_perm("archive", "read"))
        targets = License.objects.filter(
            models_q_date_window("expire_date", today, deadline)
            | models_q_date_window("next_review_date", today, deadline)
        )
        for item in targets:
            for user in recipients:
                if item.expire_date and today <= item.expire_date <= deadline:
                    if services.emit(
                        "license_expiry",
                        recipient=user,
                        context={
                            "license_name": item.name,
                            "code": item.code,
                            "expire_date": item.expire_date,
                            "days_left": (item.expire_date - today).days,
                        },
                        dedup_context=f"license:{item.pk}:{today}",
                        link=f"/archive/licenses/{item.pk}/",
                    ):
                        created += 1
                if item.next_review_date and today <= item.next_review_date <= deadline:
                    if services.emit(
                        "license_expiry",
                        recipient=user,
                        context={
                            "license_name": item.name,
                            "code": item.code,
                            "expire_date": item.next_review_date,
                            "days_left": (item.next_review_date - today).days,
                        },
                        dedup_context=f"license_review:{item.pk}:{today}",
                        link=f"/archive/licenses/{item.pk}/",
                    ):
                        created += 1
        return "证照到期/年审", created

    def _archive_contracts(self, today, deadline):
        from archive.models import Archive

        created = 0
        recipients = list(services.users_with_perm("archive", "read"))
        targets = Archive.objects.filter(
            expire_date__gte=today, expire_date__lte=deadline,
        )
        for item in targets:
            for user in recipients:
                if services.emit(
                    "archive_expiry",
                    recipient=user,
                    context={
                        "archive_name": str(item),
                        "expire_date": item.expire_date,
                        "days_left": (item.expire_date - today).days,
                    },
                    dedup_context=f"archive:{item.pk}:{today}",
                    link=f"/archive/archives/{item.pk}/",
                ):
                    created += 1
        return "档案合同到期", created

    def _borrows(self, today):
        from archive.models import BorrowRecord, LicenseBorrowRecord

        created = 0
        recipients = list(services.users_with_perm("archive", "read"))
        for model, tpl, link in (
            (BorrowRecord, "borrow_due", "/archive/borrows/"),
            (LicenseBorrowRecord, "borrow_due", "/archive/licenses/borrows/"),
        ):
            targets = model.objects.exclude(
                status=model.Status.RETURNED,
            ).filter(due_time__lte=today)
            for item in targets:
                for user in recipients:
                    if services.emit(
                        tpl,
                        recipient=user,
                        context={
                            "borrower": item.borrower,
                            "due_time": item.due_time,
                            "overdue_days": max(0, (today - item.due_time).days),
                        },
                        dedup_context=f"borrow:{model.__name__}:{item.pk}:{today}",
                        link=link,
                    ):
                        created += 1
        return "借阅到期/逾期", created


def models_q_date_window(field, start, end):
    from django.db.models import Q

    return Q(**{f"{field}__isnull": False,
                f"{field}__gte": start, f"{field}__lte": end})
