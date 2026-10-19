"""A5 审计异常行为检测（读取层）验收测试。

覆盖：
1. 具备 system.read 权限的用户可访问，构造同人 1 小时内 21 条删除 → 命中「删除爆发」；
   正常量（5 条）→ 不命中。
2. 无 system.read 权限的登录用户 → 403。
3. days 参数非法/越界（abc / 0 / 999）→ 不报错，回退默认或夹取到合法范围。

注：OperationLog.operation_time 为 auto_now_add，测试需用
``OperationLog.objects.filter(pk=...).update(operation_time=...)`` 绕过它写入历史时间。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Permission, Role

from .models import OperationLog

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
        pks = []
        for i in range(count):
            log = OperationLog.objects.create(
                operator=operator,
                module_type="测试",
                operation_type="删除",
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
        return any(row["operator"] == operator for row in rows)

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
