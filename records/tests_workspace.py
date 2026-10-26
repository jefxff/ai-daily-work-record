"""待办工作台（批次 3 / P1）与通知接入（样品终止 / 年审未通过）定向验收测试。

覆盖：
- workspace 视图：无任一来源权限 403、每日记录用户隔离、紧急度排序、
  来源筛选、页头统计基于未筛选集合；
- 样品终止：terminate_sample 触发 sample_terminated 通知、按权限定位接收人、
  dedup_context 幂等；
- 年审未通过：annual_review_create 提交 failed 触发 license_review_failed 通知，
  passed 不触发。
权限构造复用 records/tests_expiry.py 的 grant() 模式。
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Permission, Role
from notifications.models import Notification

from .views_workspace import collect_todos

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


def _mk_daily_record(user, content, due_date, status="未完成"):
    from .models import DailyRecord

    return DailyRecord.objects.create(
        user=user, department="测试部", work_content=content,
        due_date=due_date, status=status, progress=0,
    )


class LicenseTodoDedupTests(TestCase):
    """证照待办：一证一条；年审周期=0 不提醒；超回溯源不常驻。"""

    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        self.user = User.objects.create_user("ws_lic", "l@e.com", "x12345678")
        grant("archive", ("read",), self.user)
        self.today = timezone.localdate()

    def _license(self, code, expire_days=None, review_days=None, cycle=0):
        from archive.models import License

        return License.objects.create(
            name=f"证照{code}", code=code,
            expire_date=(
                self.today + timedelta(days=expire_days)
                if expire_days is not None else None
            ),
            next_review_date=(
                self.today + timedelta(days=review_days)
                if review_days is not None else None
            ),
            review_cycle=cycle,
        )

    def _license_todos(self):
        return [
            t for t in collect_todos(self.user, today=self.today)
            if t["source"] == "license_due"
        ]

    def test_one_row_per_license_when_both_dates_due(self):
        self._license("LIC-A", expire_days=-5, review_days=-40, cycle=12)
        todos = self._license_todos()
        self.assertEqual(len(todos), 1, "同一证照不应同时出有效期与年审两条")
        self.assertEqual(todos[0]["days_left"], -40, "应取更早的日期作为期限")

    def test_zero_review_cycle_has_no_review_todo(self):
        self._license("LIC-B", expire_days=10, review_days=-20, cycle=0)
        todos = self._license_todos()
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]["days_left"], 10, "年审周期=0 时不应产生年审提醒")

    def test_old_overdue_license_outside_lookback(self):
        self._license("LIC-C", expire_days=-120)
        self.assertEqual(self._license_todos(), [], "超出回溯窗口的陈年证照不应常驻待办")


class TerminatedSampleNotTodoTests(TestCase):
    """已终止的试样是终态，不需要人推进，不应进入待办工作台。"""

    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        self.user = User.objects.create_user("ws_sample", "s@e.com", "x12345678")
        grant("samples", ("read",), self.user)
        self.today = timezone.localdate()

    def test_terminated_sample_not_collected(self):
        from samples.models import Sample

        Sample.objects.create(
            sample_no="S-TERM-1", material_name="铝合金", model="M-1", spec="spec",
            incoming_qty=1, sample_date=self.today,
            status=Sample.Status.TERMINATED, terminated_at=timezone.now(),
        )
        todos = collect_todos(self.user, today=self.today)
        self.assertEqual(
            [t for t in todos if t["source"] == "sample_terminated"], [],
            "已终止试样不应作为待办",
        )


class WorkspacePermissionTests(TestCase):
    """无任一来源模块 read 权限 → 403；有任一权限 → 200。"""

    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        self.norole = User.objects.create_user("ws_norole", "n@e.com", "x12345678")

    def test_no_permission_forbidden(self):
        self.client.force_login(self.norole)
        resp = self.client.get(reverse("workspace"))
        self.assertEqual(resp.status_code, 403)

    def test_single_source_permission_ok(self):
        grant("daily_record", ("read",), self.norole)
        self.client.force_login(self.norole)
        resp = self.client.get(reverse("workspace"))
        self.assertEqual(resp.status_code, 200)


class WorkspaceDailyRecordIsolationTests(TestCase):
    """每日记录待办按用户隔离：A 的未完成记录不出现在 B 的工作台。"""

    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        today = timezone.localdate()
        self.a = User.objects.create_user("ws_a", "a@e.com", "x12345678")
        self.b = User.objects.create_user("ws_b", "b@e.com", "x12345678")
        grant("daily_record", ("read",), self.a)
        grant("daily_record", ("read",), self.b)
        _mk_daily_record(self.a, "A 的待办事项", today - timedelta(days=1))
        _mk_daily_record(self.b, "B 的待办事项", today + timedelta(days=10))

    def test_records_isolated_per_user(self):
        todos_a = collect_todos(self.a)
        titles_a = [t["title"] for t in todos_a]
        self.assertIn("A 的待办事项", titles_a)
        self.assertNotIn("B 的待办事项", titles_a)

        todos_b = collect_todos(self.b)
        titles_b = [t["title"] for t in todos_b]
        self.assertIn("B 的待办事项", titles_b)
        self.assertNotIn("A 的待办事项", titles_b)

    def test_overdue_sorts_before_low_urgency(self):
        """同一用户：逾期（高）排在远期（低）之前。"""
        today = timezone.localdate()
        _mk_daily_record(self.a, "A 的远期事项", today + timedelta(days=30))
        todos = collect_todos(self.a)
        self.assertGreaterEqual(len(todos), 2)
        self.assertEqual(todos[0]["title"], "A 的待办事项")
        self.assertEqual(todos[0]["urgency"], "高")
        urgencies = [t["urgency"] for t in todos]
        self.assertEqual(urgencies, sorted(urgencies, key={"高": 0, "中": 1, "低": 2}.get))


class WorkspaceFilterAndCountsTests(TestCase):
    """来源筛选收窄列表；页头统计 counts/total 基于未筛选集合。"""

    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        today = timezone.localdate()
        self.user = User.objects.create_user("ws_f", "f@e.com", "x12345678")
        grant("archive", ("read",), self.user)
        grant("daily_record", ("read",), self.user)

        from archive.models import Archive, BorrowRecord

        arch = Archive.objects.create(code="A-WS-1", name="测试合同")
        BorrowRecord.objects.create(
            archive=arch, borrower="张三", due_time=today - timedelta(days=2),
        )
        _mk_daily_record(self.user, "远期每日记录", today + timedelta(days=30))

    def test_source_filter_narrows_list(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("workspace"), {"source": "borrow_overdue"})
        self.assertEqual(resp.status_code, 200)
        todos = resp.context["todos"].object_list
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]["source"], "borrow_overdue")
        self.assertEqual(todos[0]["urgency"], "高")

    def test_counts_based_on_unfiltered_set(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("workspace"), {"source": "borrow_overdue"})
        counts = resp.context["counts"]
        self.assertEqual(counts["高"], 1, "借阅逾期为高")
        self.assertEqual(counts["低"], 1, "远期每日记录为低，筛选后仍计入统计")
        self.assertEqual(resp.context["total"], 2)


class SampleTerminatedNotifyTests(TestCase):
    """样品终止 → samples.read 用户收到 sample_terminated 通知（幂等）。"""

    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        self.receiver = User.objects.create_user("ws_smp", "s@e.com", "x12345678")
        grant("samples", ("read",), self.receiver)
        self.stranger = User.objects.create_user("ws_smp2", "s2@e.com", "x12345678")

        from samples.models import Sample

        self.sample = Sample.objects.create(
            sample_no="WS-T-001", material_name="测试物料", model="M-1",
            sample_date=timezone.localdate(),
        )

    def test_terminate_emits_notification(self):
        from samples import services

        # 通知已改为 transaction.on_commit 触发（审查 H2）：测试需捕获提交回调，
        # 否则 TestCase 的外层事务永不提交，通知永远不会发。
        with self.captureOnCommitCallbacks(execute=True):
            services.terminate_sample(self.sample, "测试员", "测试终止原因")
        qs = Notification.objects.filter(template__code="sample_terminated")
        self.assertEqual(qs.filter(recipient=self.receiver).count(), 1)
        self.assertEqual(qs.filter(recipient=self.stranger).count(), 0)
        n = qs.get(recipient=self.receiver)
        self.assertEqual(n.link, f"/samples/{self.sample.pk}/")

    def test_dedup_context_idempotent(self):
        from samples.services import notify_sample_terminated

        notify_sample_terminated(self.sample)
        notify_sample_terminated(self.sample)
        qs = Notification.objects.filter(
            template__code="sample_terminated", recipient=self.receiver,
        )
        self.assertEqual(qs.count(), 1, "同 dedup_context 重复触发只落库一条")


class LicenseReviewFailedNotifyTests(TestCase):
    """年审登记 failed → archive.read 用户收到 license_review_failed 通知。"""

    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        self.receiver = User.objects.create_user("ws_lic", "l@e.com", "x12345678")
        grant("archive", ("read", "create"), self.receiver)

        from archive.models import License

        self.lic = License.objects.create(name="测试证照", code="LIC-WS-1")

    def _post_review(self, result):
        return self.client.post(
            reverse("archive:annual_review_create", args=[self.lic.pk]),
            {"review_date": str(timezone.localdate()), "result": result,
             "reviewer": "张三", "new_expire_date": "", "remark": ""},
        )

    def test_failed_result_emits_notification(self):
        self.client.force_login(self.receiver)
        # on_commit 语义：测试内需捕获提交回调（审查 H2 的配套调整）
        with self.captureOnCommitCallbacks(execute=True):
            resp = self._post_review("failed")
        self.assertEqual(resp.status_code, 302)
        qs = Notification.objects.filter(
            template__code="license_review_failed", recipient=self.receiver,
        )
        self.assertEqual(qs.count(), 1)
        self.assertEqual(qs.get().link, f"/archive/licenses/{self.lic.pk}/")

    def test_passed_result_no_notification(self):
        self.client.force_login(self.receiver)
        resp = self._post_review("passed")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            Notification.objects.filter(template__code="license_review_failed").count(), 0,
        )
