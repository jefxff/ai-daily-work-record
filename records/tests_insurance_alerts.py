"""保险续保预警（阶段 3）定向测试：聚合口径 / 提醒去重 / 续保后止损。"""

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Permission, Role
from notifications.models import Notification

from .insurance_services import attach_record, renewal_alerts
from .models import Insurance, InsuranceSubject

User = get_user_model()


def grant(user, module="insurance", actions=("read",)):
    """给用户授予模块权限；角色按用户隔离，避免测试间权限串味。"""
    role, _ = Role.objects.get_or_create(
        code=f"t_{module}_{user.pk}", defaults={"name": f"测试-{module}-{user.pk}", "is_active": True}
    )
    for action in actions:
        perm, _ = Permission.objects.get_or_create(
            module=module, action=action,
            defaults={"code": f"{module}:{action}", "name": f"{module}-{action}"},
        )
        role.permissions.add(perm)
    role.users.add(user)
    if hasattr(user, "_rbac_codes_cache"):
        del user._rbac_codes_cache
    cache.clear()


def make_insurance(title, end, premium="1000.00", issue=None, company="太平洋保险"):
    return Insurance.objects.create(
        company_name=company, title=title, premium=Decimal(premium),
        issue_date=issue or date(end.year - 1, end.month, end.day),
        end_date=end, department="行政部",
    )


class RenewalAlertsTests(TestCase):
    """renewal_alerts：按标的 + 最新年度聚合，同年度多条只算一个。"""

    def test_same_year_records_aggregate_to_one_subject(self):
        today = timezone.localdate()
        end = today + timedelta(days=5)
        for title, premium in (("车险 粤B8X66D", "1000"), ("车险 粤B8X66D 商业险", "500")):
            attach_record(make_insurance(title, end, premium=premium))
        alerts = renewal_alerts(days=30, today=today)
        self.assertEqual(alerts["due_count"], 1)
        self.assertEqual(alerts["lapsed_count"], 0)
        self.assertEqual(alerts["due"][0]["year"].premium, Decimal("1500.00"))

    def test_lapsed_and_due_are_split(self):
        today = timezone.localdate()
        attach_record(make_insurance("车险 粤A12345", today - timedelta(days=5)))
        attach_record(make_insurance("车险 粤B12345", today + timedelta(days=10)))
        alerts = renewal_alerts(days=30, today=today)
        self.assertEqual(alerts["lapsed_count"], 1)
        self.assertEqual(alerts["lapsed"][0]["overdue_days"], 5)
        self.assertEqual(alerts["due_count"], 1)
        self.assertEqual(alerts["due"][0]["days_left"], 10)

    def test_outside_window_not_included(self):
        today = timezone.localdate()
        attach_record(make_insurance("车险 粤C12345", today + timedelta(days=90)))
        alerts = renewal_alerts(days=30, today=today)
        self.assertEqual(alerts["due_count"], 0)
        self.assertEqual(alerts["lapsed_count"], 0)


class CheckInsuranceRenewalCommandTests(TestCase):
    """定时任务：临期按里程碑去重，应续未续按周去重，续保后不再报断保。"""

    def setUp(self):
        self.user = User.objects.create_user("ins_user", password="x12345678")
        grant(self.user, actions=("read",))

    def test_due_notice_sent_once_per_year(self):
        today = timezone.localdate()
        end = today + timedelta(days=2)
        attach_record(make_insurance("车险 粤B8X66D", end))
        attach_record(make_insurance("车险 粤B8X66D 商业险", end))
        call_command("check_insurance_renewal", verbosity=0)
        call_command("check_insurance_renewal", verbosity=0)
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.user, template__code="insurance_expiry",
            ).count(),
            1,
        )

    def test_lapsed_notice_sent_once_per_week(self):
        today = timezone.localdate()
        attach_record(make_insurance("车险 粤D12345", today - timedelta(days=5)))
        call_command("check_insurance_renewal", verbosity=0)
        call_command("check_insurance_renewal", verbosity=0)
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.user, template__code="insurance_lapsed",
            ).count(),
            1,
        )

    def test_renewed_subject_stops_lapsed_notice(self):
        today = timezone.localdate()
        attach_record(make_insurance("车险 粤E12345", today - timedelta(days=5)))
        attach_record(make_insurance(
            "车险 粤E12345", today + timedelta(days=360),
            issue=today + timedelta(days=1),
        ))
        call_command("check_insurance_renewal", verbosity=0)
        self.assertFalse(
            Notification.objects.filter(
                template__code="insurance_lapsed",
            ).exists()
        )

    def test_no_permission_no_notice(self):
        self.user.rbac_roles.clear()
        cache.clear()
        if hasattr(self.user, "_rbac_codes_cache"):
            del self.user._rbac_codes_cache
        today = timezone.localdate()
        attach_record(make_insurance("车险 粤F12345", today + timedelta(days=3)))
        call_command("check_insurance_renewal", verbosity=0)
        self.assertEqual(
            Notification.objects.filter(template__code="insurance_expiry").count(), 0,
        )


class OneOffSubjectTests(TestCase):
    """一次性保险：标记后不再进入预警与报告，可恢复监控。"""

    def setUp(self):
        self.user = User.objects.create_user("one_off", password="x12345678")
        grant(self.user, actions=("read", "create", "update"))
        today = timezone.localdate()
        self.record = make_insurance("建筑工程险", today - timedelta(days=30))
        attach_record(self.record)
        self.key = "noncar:建筑工程险"

    def test_mark_excludes_from_alerts_and_report(self):
        c = Client()
        c.force_login(self.user)
        resp = c.post(reverse("insurance_subject_one_off"),
                      {"key": self.key, "action": "mark", "next": "report"})
        self.assertRedirects(resp, reverse("insurance_renewal_report"))
        self.assertTrue(
            InsuranceSubject.objects.get(insurance_type="建筑工程险").is_one_off)

        alerts = renewal_alerts(days=30)
        self.assertEqual(alerts["lapsed_count"], 0)

        call_command("check_insurance_renewal", verbosity=0)
        self.assertFalse(Notification.objects.filter(
            template__code="insurance_lapsed").exists())

        page = c.get(reverse("insurance_renewal_report"))
        self.assertNotContains(page, self.key)

    def test_unmark_restores_alerts(self):
        c = Client()
        c.force_login(self.user)
        c.post(reverse("insurance_subject_one_off"),
               {"key": self.key, "action": "mark", "next": "report"})
        resp = c.post(reverse("insurance_subject_one_off"),
                      {"key": self.key, "action": "unmark", "next": "detail"})
        self.assertIn("/insurance/subjects/detail/", resp.url)
        self.assertFalse(
            InsuranceSubject.objects.get(insurance_type="建筑工程险").is_one_off)
        self.assertEqual(renewal_alerts(days=30)["lapsed_count"], 1)

    def test_mark_requires_update_permission(self):
        viewer = User.objects.create_user("one_off_viewer", password="x12345678")
        grant(viewer, actions=("read",))
        c = Client()
        c.force_login(viewer)
        self.assertEqual(
            c.post(reverse("insurance_subject_one_off"),
                   {"key": self.key, "action": "mark"}).status_code,
            403,
        )

    def test_get_not_allowed(self):
        c = Client()
        c.force_login(self.user)
        self.assertEqual(
            c.get(reverse("insurance_subject_one_off"), {"key": self.key}).status_code,
            404,
        )

    def test_audit_log_written(self):
        from records.models import OperationLog

        c = Client()
        c.force_login(self.user)
        c.post(reverse("insurance_subject_one_off"),
               {"key": self.key, "action": "mark", "next": "report"})
        self.assertTrue(OperationLog.objects.filter(
            module_type="保险", operation_type="标记一次性").exists())


class RenewalReportPageTests(TestCase):
    """续保报告页：有 create 权限显示一键续保，无 read 权限 403。"""

    def setUp(self):
        self.user = User.objects.create_user("ins_page", password="x12345678")

    def test_renew_button_visible_with_create_perm(self):
        grant(self.user, actions=("read", "create"))
        today = timezone.localdate()
        attach_record(make_insurance("车险 粤G12345", today + timedelta(days=5)))
        c = Client()
        c.force_login(self.user)
        resp = c.get(reverse("insurance_renewal_report"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "/insurance/renew/?from_record=")

    def test_read_only_user_sees_no_renew_button(self):
        grant(self.user, actions=("read",))
        today = timezone.localdate()
        attach_record(make_insurance("车险 粤H12345", today + timedelta(days=5)))
        c = Client()
        c.force_login(self.user)
        resp = c.get(reverse("insurance_renewal_report"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, f"/insurance/renew/?from_record=")

    def test_without_read_perm_403(self):
        c = Client()
        c.force_login(self.user)
        self.assertEqual(c.get(reverse("insurance_renewal_report")).status_code, 403)
