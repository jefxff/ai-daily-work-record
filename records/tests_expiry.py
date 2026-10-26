"""到期预警（批次 A1）定向验收测试。

覆盖：跨模块页权限判定（403）、按来源模块过滤（仅 archive 时不含保险）、
严重度分档、导出空态。
权限构造复用 records/tests_hardening.py 的 grant() 模式。
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Permission, Role

from . import expiry
from .models import Insurance

User = get_user_model()


def grant(module, actions=("read",), user=None):
    """给用户授予某模块的动作权限（按需建角色）。"""
    role, _ = Role.objects.get_or_create(
        code=f"t_{module}", defaults={"name": f"测试-{module}", "is_active": True}
    )
    for action in actions:
        perm, _ = Permission.objects.get_or_create(
            module=module,
            action=action,
            defaults={"code": f"{module}:{action}", "name": f"{module}-{action}"},
        )
        role.permissions.add(perm)
    if user is not None:
        role.users.add(user)
        # _codes() 会缓存到 user 实例上，切换用户后必须清缓存
        if hasattr(user, "_rbac_codes_cache"):
            del user._rbac_codes_cache
    cache.clear()
    return role


def _item_kinds(response):
    """把 by_day 分组里的事项 kind 拉平成列表。"""
    kinds = []
    for group in response.context["by_day"]:
        for item in group["items"]:
            kinds.append(item["kind"])
    return kinds


class ExpiryCalendarPermissionTests(TestCase):
    """跨模块页：无 insurance/archive read 权限 → 403。"""

    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        self.norole = User.objects.create_user("exp_norole", "n@e.com", "x12345678")

    def test_no_source_permission_forbidden(self):
        self.client.force_login(self.norole)
        resp = self.client.get(reverse("expiry_calendar"))
        self.assertEqual(resp.status_code, 403)
        # 403 页由 accounts/403.html 渲染
        self.assertContains(resp, "没有访问该内容的权限", status_code=403)


class ExpiryCalendarSourceFilterTests(TestCase):
    """仅有 archive read 权限：返回 200，且不含保险来源条目。"""

    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        self.archive_only = User.objects.create_user("exp_arch", "a@e.com", "x12345678")
        grant("archive", ("read",), self.archive_only)

        today = timezone.localdate()
        Insurance.objects.create(
            company_name="测试保险公司", title="财产险", premium=100,
            issue_date=today - timedelta(days=30), end_date=today + timedelta(days=10),
            department="行政部",
        )
        from archive.models import Archive

        Archive.objects.create(code="A-EXP-1", name="测试合同", expire_date=today + timedelta(days=10))

    def test_archive_only_sees_no_insurance_items(self):
        self.client.force_login(self.archive_only)
        resp = self.client.get(reverse("expiry_calendar"))
        self.assertEqual(resp.status_code, 200)
        kinds = _item_kinds(resp)
        self.assertIn("archive_contract", kinds, "archive read 用户应能看到合同到期项")
        self.assertNotIn("insurance", kinds, "无 insurance read 权限不应看到保险来源")


class InsuranceRenewalExpiryTests(TestCase):
    """保险来源按「标的最新保单年度」聚合：已续保的旧年度不再报已过期。"""

    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        self.user = User.objects.create_user("exp_ins", "i@e.com", "x12345678")
        grant("insurance", ("read",), self.user)
        self.today = timezone.localdate()

    def _policy(self, title, end_days):
        end = self.today + timedelta(days=end_days)
        return Insurance.objects.create(
            company_name="测试保险公司", title=title, premium=100,
            issue_date=end - timedelta(days=365), end_date=end,
            department="行政部",
        )

    def _insurance_items(self):
        return [it for it in expiry.collect_expiry_items(self.user) if it["kind"] == "insurance"]

    def test_renewed_car_is_not_expired(self):
        self._policy("车险 粤BC7J68", -87)          # 旧年度：已过期
        self._policy("车险 粤BC7J68", 300)          # 续保后的新年度
        self.assertEqual(self._insurance_items(), [], "已续保的标的应只按最新年度判断，不应再报已过期")

    def test_latest_year_reported_when_expiring(self):
        self._policy("车险 粤BC7J68", -87)
        self._policy("车险 粤BC7J68", 20)
        items = self._insurance_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "粤BC7J68")
        self.assertEqual(items[0]["days_left"], 20)

    def test_one_off_subject_is_ignored(self):
        from .models import InsuranceSubject

        InsuranceSubject.objects.create(
            kind="car", label="粤B00001", plate="粤B00001",
            insurance_type="车险", is_one_off=True,
        )
        self._policy("车险 粤B00001", 10)
        self.assertEqual(self._insurance_items(), [], "一次性标的不应进入续保预警")


class ArchiveRenewalStateTests(TestCase):
    """一次性合同到期后标记「已结束」：不再进入到期预警，可恢复。"""

    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        self.user = User.objects.create_user("exp_arc", "ar@e.com", "x12345678")
        grant("archive", ("read", "update"), self.user)
        self.today = timezone.localdate()

    def _archive(self, code, expire_days, state="pending"):
        from archive.models import Archive

        return Archive.objects.create(
            code=code, name=f"合同-{code}",
            expire_date=self.today + timedelta(days=expire_days),
            renewal_state=state,
        )

    def _contract_titles(self):
        return [
            it["title"] for it in expiry.collect_expiry_items(self.user)
            if it["kind"] == "archive_contract"
        ]

    def test_ended_and_renewed_contracts_are_hidden(self):
        self._archive("A-P", 20)
        self._archive("A-E", -30, "ended")
        self._archive("A-R", -30, "renewed")
        self.assertEqual(self._contract_titles(), ["合同-A-P"])

    def test_mark_ended_hides_contract(self):
        arc = self._archive("A-M", -10)
        self.assertEqual(self._contract_titles(), ["合同-A-M"])

        self.client.force_login(self.user)
        resp = self.client.post(
            reverse("archive:archive_set_renewal", args=[arc.pk]), {"state": "ended"}
        )
        self.assertEqual(resp.status_code, 302)
        arc.refresh_from_db()
        self.assertEqual(arc.renewal_state, "ended")
        self.assertEqual(self._contract_titles(), [])

    def test_restore_pending_brings_contract_back(self):
        arc = self._archive("A-B", -10, "ended")
        self.client.force_login(self.user)
        self.client.post(
            reverse("archive:archive_set_renewal", args=[arc.pk]), {"state": "pending"}
        )
        arc.refresh_from_db()
        self.assertEqual(arc.renewal_state, "pending")
        self.assertEqual(self._contract_titles(), ["合同-A-B"])

    def test_invalid_state_rejected(self):
        arc = self._archive("A-X", 10)
        self.client.force_login(self.user)
        resp = self.client.post(
            reverse("archive:archive_set_renewal", args=[arc.pk]), {"state": "bogus"}
        )
        self.assertEqual(resp.status_code, 400)
        arc.refresh_from_db()
        self.assertEqual(arc.renewal_state, "pending")


class SeverityTests(TestCase):
    """直接单测 records.expiry._severity 的分档边界。"""

    def test_severity_bands(self):
        lead_days = 30
        self.assertEqual(expiry._severity(-1, lead_days), "danger")
        self.assertEqual(expiry._severity(3, lead_days), "danger")
        self.assertEqual(expiry._severity(lead_days, lead_days), "warning")
        self.assertEqual(expiry._severity(lead_days + 1, lead_days), "success")


class ExpiryExportEmptyStateTests(TestCase):
    """当月无数据时导出返回空态文案。"""

    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        self.user = User.objects.create_user("exp_export", "e@e.com", "x12345678")
        grant("archive", ("read",), self.user)

    def test_export_empty_returns_placeholder(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("expiry_export"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content.decode(), "本月暂无到期事项")


class BorrowDueSourceTests(TestCase):
    """未归还借阅的应还日期进入到期聚合（days_left 实时计算，不依赖 status 刷新）。"""

    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        self.user = User.objects.create_user("exp_borrow", "b@e.com", "x12345678")
        grant("archive", ("read",), self.user)

        from archive.models import Archive, BorrowRecord

        today = timezone.localdate()
        self.archive = Archive.objects.create(code="A-BOR-1", name="逾期合同档案")
        BorrowRecord.objects.create(
            archive=self.archive, borrower="张三", due_time=today - timedelta(days=3),
        )
        BorrowRecord.objects.create(
            archive=Archive.objects.create(code="A-BOR-2", name="已还档案"),
            borrower="李四", due_time=today - timedelta(days=5),
            return_time=timezone.now(),
        )

    def test_open_borrow_visible_returned_hidden(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("expiry_calendar"))
        kinds = _item_kinds(resp)
        self.assertIn("borrow_due", kinds, "未归还借阅应出现在到期聚合中")
        self.assertEqual(kinds.count("borrow_due"), 1, "已归还的借阅不应出现")

    def test_overdue_borrow_is_danger(self):
        items = expiry.collect_expiry_items(self.user)
        borrow = next(it for it in items if it["kind"] == "borrow_due")
        self.assertLess(borrow["days_left"], 0)
        self.assertEqual(borrow["severity"], "danger")


class MonthCalendarTests(TestCase):
    """月历网格与点选日期过滤。"""

    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        self.user = User.objects.create_user("exp_cal", "c@e.com", "x12345678")
        grant("archive", ("read",), self.user)

        from archive.models import Archive

        self.today = timezone.localdate()
        Archive.objects.create(
            code="A-CAL-1", name="本月到期合同", expire_date=self.today + timedelta(days=5),
        )

    def test_month_weeks_cover_today_and_count(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("expiry_calendar"))
        self.assertEqual(resp.status_code, 200)
        weeks = resp.context["month_weeks"]
        cells = [c for week in weeks for c in week]
        today_cell = next(c for c in cells if c["is_today"])
        self.assertTrue(today_cell["in_month"])
        target = self.today + timedelta(days=5)
        target_cell = next(c for c in cells if c["date"] == target)
        self.assertEqual(target_cell["count"], 1)
        self.assertIn("archive_contract", target_cell["kinds"])

    def test_day_filter_narrows_by_day(self):
        self.client.force_login(self.user)
        target = self.today + timedelta(days=5)
        resp = self.client.get(reverse("expiry_calendar"), {"day": target.strftime("%Y-%m-%d")})
        by_day = resp.context["by_day"]
        self.assertEqual(len(by_day), 1)
        self.assertEqual(by_day[0]["date"], target)

    def test_out_of_window_day_ignored(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("expiry_calendar"), {"day": "2035-01-01"})
        self.assertIsNone(resp.context["selected_day"])

    def test_month_nav_clamped_to_window(self):
        """ym 超出数据窗口时钳制回窗口内月份。"""
        self.client.force_login(self.user)
        resp = self.client.get(reverse("expiry_calendar"), {"ym": "2035-01"})
        month_start = resp.context["month_start"]
        horizon_end = self.today + timedelta(days=expiry.DEFAULT_HORIZON_DAYS)
        self.assertEqual(month_start, horizon_end.replace(day=1))
