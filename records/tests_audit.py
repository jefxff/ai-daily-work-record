"""A5 审计异常行为检测（读取层）验收测试。

覆盖：
1. 具备 system.read 权限的用户可访问，构造同人 1 小时内 21 条删除 → 命中「删除爆发」；
   正常量（5 条）→ 不命中。
2. 无 system.read 权限的登录用户 → 403。
3. days 参数非法/越界（abc / 0 / 999）→ 不报错，回退默认或夹取到合法范围。

注：OperationLog.operation_time 为 auto_now_add，测试需用
``OperationLog.objects.filter(pk=...).update(operation_time=...)`` 绕过它写入历史时间。
"""

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Permission, Role

from .models import Decoration, OperationLog

PWD = "X12345678"


def grant_system_read(user):
    """给用户授予 system.read（按需建角色）。"""
    role, _ = Role.objects.get_or_create(
        code="t_system", defaults={"name": "测试-system", "is_active": True}
    )
    perm, _ = Permission.objects.get_or_create(
        module="system",
        action="read",
        defaults={"code": "system:read", "name": "system-read"},
    )
    role.permissions.add(perm)
    role.users.add(user)
    # _codes() 会缓存到 user 实例上，授权后必须清缓存
    if hasattr(user, "_rbac_codes_cache"):
        del user._rbac_codes_cache
    cache.clear()
    return role


class AuditAnomaliesTests(TestCase):
    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        self.allowed = User.objects.create_user("audit_ok", "a@e.com", PWD, is_active=True)
        grant_system_read(self.allowed)
        self.denied = User.objects.create_user("audit_no", "n@e.com", PWD, is_active=True)

    def tearDown(self):
        cache.clear()

    # ---------- 辅助 ----------
    def _seed_deletes(self, operator, count, when):
        """构造 count 条删除留痕，并把 operation_time 回写为指定时间（绕过 auto_now_add）。"""
        self._seed_logs(operator, "删除", count, when)

    def _seed_logs(self, operator, op_type, count, when):
        """构造 count 条指定类型的留痕，并把 operation_time 回写为指定时间。"""
        pks = []
        for i in range(count):
            log = OperationLog.objects.create(
                operator=operator,
                module_type="测试",
                operation_type=op_type,
                object_identifier=f"obj-{i}",
                change_summary="x",
            )
            pks.append(log.pk)
        OperationLog.objects.filter(pk__in=pks).update(operation_time=when)

    def _login(self, user):
        c = Client()
        c.force_login(user)
        return c

    def _hits_operator(self, rows, operator):
        # 分组键是 actor（账号优先、展示名兜底）：测试里没有账号，回退到 operator
        return any((row.get("actor") or row.get("operator")) == operator for row in rows)

    # ---------- 1. 命中 / 不命中 ----------
    def test_delete_burst_hit_and_miss(self):
        url = reverse("audit_anomalies")
        when = timezone.now() - timedelta(minutes=5)

        # 21 条 → 命中「删除爆发」
        self._seed_deletes("suspect21", 21, when)
        resp = self._login(self.allowed).get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(self._hits_operator(resp.context["delete_bursts"], "suspect21"))

        # 5 条（同窗口）→ 不命中
        self._seed_deletes("suspect5", 5, when)
        resp = self._login(self.allowed).get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self._hits_operator(resp.context["delete_bursts"], "suspect5"))

    # ---------- 批次B：导出爆发 ----------
    def test_export_burst_hit_and_miss(self):
        """同一操作人 1 小时内导出 11 次 → 命中；5 次 → 不命中。"""
        url = reverse("audit_anomalies")
        when = timezone.now() - timedelta(minutes=5)

        self._seed_logs("exporter11", "导出", 11, when)
        resp = self._login(self.allowed).get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(self._hits_operator(resp.context["export_bursts"], "exporter11"))

        self._seed_logs("exporter5", "导出", 5, when)
        resp = self._login(self.allowed).get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(self._hits_operator(resp.context["export_bursts"], "exporter5"))

    # ---------- 2. 权限 ----------
    def test_forbidden_without_system_read(self):
        resp = self._login(self.denied).get(reverse("audit_anomalies"))
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_redirects_to_login(self):
        resp = Client().get(reverse("audit_anomalies"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp.url)

    # ---------- 3. days 参数容错 ----------
    def test_invalid_days_falls_back_or_clamps(self):
        c = self._login(self.allowed)
        url = reverse("audit_anomalies")

        resp = c.get(url, {"days": "abc"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["days"], 7)

        resp = c.get(url, {"days": "0"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["days"], 1)

        resp = c.get(url, {"days": "999"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["days"], 90)

    def test_empty_all_when_no_rows(self):
        resp = self._login(self.allowed).get(reverse("audit_anomalies"))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["empty_all"])


class OperationSnapshotTests(TestCase):
    """批次B：OperationLog.before_json/after_json 快照落库与字段级变更明细。"""

    def setUp(self):
        call_command("sync_rbac", verbosity=0)
        self.operator = User.objects.create_user("snap_user", "s@e.com", PWD, is_active=True)

    def _request(self):
        request = RequestFactory().post("/")
        request.user = self.operator
        return request

    def test_snapshot_none_returns_none(self):
        from .audit import snapshot

        self.assertIsNone(snapshot(None))

    def test_snapshot_is_json_safe_and_truncates(self):
        from .audit import MAX_SNAPSHOT_CHARS, snapshot

        long_text = "字" * (MAX_SNAPSHOT_CHARS + 50)
        obj = Decoration.objects.create(
            project_name="快照测试", constructor="甲", start_date=date(2026, 1, 1),
            project_address=long_text, final_price=Decimal("10.00"),
        )
        snap = snapshot(obj)
        self.assertIsInstance(snap, dict)
        self.assertEqual(snap["project_name"], "快照测试")
        self.assertEqual(snap["start_date"], "2026-01-01")   # date → isoformat
        self.assertEqual(snap["final_price"], "10.00")        # Decimal → str
        self.assertEqual(len(snap["project_address"]), MAX_SNAPSHOT_CHARS + 1)
        self.assertTrue(snap["project_address"].endswith("…"))

    def test_log_operation_persists_before_after(self):
        from .audit import log_operation

        log_operation(
            self._request(), "装修", "更新", "装修#1", "改动",
            before={"project_name": "旧名", "final_price": "10"},
            after={"project_name": "新名", "final_price": "10"},
        )
        log = OperationLog.objects.get()
        self.assertEqual(log.before_json["project_name"], "旧名")
        self.assertEqual(log.after_json["project_name"], "新名")
        # 仅前后不一致的字段进入变更明细
        self.assertEqual(log.change_diff, [("project_name", "旧名", "新名")])

    def test_change_diff_empty_without_snapshot(self):
        from .audit import log_operation

        log_operation(self._request(), "到期预警", "导出", "[到期预警]下的导出", "导出 3 条")
        self.assertEqual(OperationLog.objects.get().change_diff, [])

    def test_change_diff_caps_items(self):
        from .audit import log_operation

        limit = OperationLog.MAX_DIFF_ITEMS
        before = {f"f{i}": 0 for i in range(limit + 10)}
        after = {f"f{i}": 1 for i in range(limit + 10)}
        log_operation(self._request(), "测试", "更新", "测试#1", "全字段变更",
                      before=before, after=after)
        self.assertEqual(len(OperationLog.objects.get().change_diff), limit)

    def test_change_diff_marks_create_and_delete_side(self):
        from .audit import log_operation

        log_operation(self._request(), "每日记录", "新增", "每日记录#1", "新增",
                      before=None, after={"work_content": "内容"})
        self.assertEqual(OperationLog.objects.get().change_diff,
                         [("work_content", None, "内容")])

        log_operation(self._request(), "每日记录", "删除", "每日记录#2", "删除",
                      before={"work_content": "内容"}, after=None)
        log = OperationLog.objects.filter(operation_type="删除").get()
        self.assertEqual(log.change_diff, [("work_content", "内容", None)])


class AuditVocabTests(TestCase):
    """留痕词表单一事实源（一致性审计 P0-1 / P0-2）。

    锁定三件事：
    1. 写入时把同义词折叠成标准词（历史行不改，新行一致）；
    2. 查询/规则展开同义词，历史行照样命中（不需要数据回填）；
    3. 操作人账号单独落库：展示名口径不同也能按同一个人聚合。
    """

    def test_write_normalizes_synonyms(self):
        from records.audit import write_operation

        write_operation("tester", "样品", "更新", "样品#1", "旧词写入")
        write_operation("tester", "档案", "文档创建", "档案#1", "旧词写入")
        log = OperationLog.objects.latest("id")
        self.assertEqual(log.operation_type, "新增")
        self.assertEqual(log.module_type, "档案管理")
        prev = OperationLog.objects.exclude(pk=log.pk).latest("id")
        self.assertEqual(prev.operation_type, "编辑")     # 更新 → 编辑
        self.assertEqual(prev.module_type, "试样管理")    # 样品 → 试样管理

    def test_expand_covers_legacy_values(self):
        from records import audit_vocab as v

        self.assertIn("编辑", v.expand_action("编辑"))
        self.assertIn("更新", v.expand_action("编辑"), "历史行里的「更新」必须仍可被筛出")
        self.assertIn("文档编辑", v.expand_action("编辑"))
        self.assertIn("删除", v.expand_action("删除"))
        self.assertIn("档案", v.expand_module("档案管理"))
        self.assertIn("导出", v.EXPORT_ACTIONS)

    def test_filter_callables_match_legacy_rows(self):
        from records import audit_vocab as v

        OperationLog.objects.create(operator="x", module_type="档案", operation_type="更新",
                                    object_identifier="档案#1", change_summary="历史行")
        qs = OperationLog.objects.all()
        self.assertEqual(v.filter_by_module(qs, "档案管理").count(), 1)
        self.assertEqual(v.filter_by_action(qs, "编辑").count(), 1)
        self.assertEqual(v.filter_by_action(qs, "").count(), 1)   # 空值不过滤

    def test_operator_username_recorded_even_with_real_name_display(self):
        """展示名记姓名时，账号列仍要落下 —— 否则聚合会把一个人拆成两个。"""
        from accounts.models import UserProfile

        user = User.objects.create_user("audit_p1", "p1@e.com", PWD, is_active=True)
        UserProfile.objects.create(user=user, real_name="审计一号")

        rf = RequestFactory()
        req = rf.post("/x/")
        req.user = user
        from records.audit import log_operation

        log_operation(req, "档案管理", "编辑", "档案#9", "按姓名展示", prefer_real_name=True)
        log = OperationLog.objects.latest("id")
        self.assertEqual(log.operator, "审计一号")
        self.assertEqual(log.operator_username, "audit_p1")

    def test_industry_vocab_includes_notification_module(self):
        """下拉里必须有「通知中心」，否则通知模块的留痕在页面上筛不出来。"""
        from records import audit_vocab as v

        self.assertIn("通知中心", v.MODULE_CHOICES)
        self.assertIn("任务配置", v.ACTION_CHOICES)
        self.assertIn("重置密码", v.ACTION_CHOICES)
