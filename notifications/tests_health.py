"""系统健康面板（A6）测试：页面权限 / 探测结构与 API 一致性。

运行：python manage.py test notifications.tests_health -v 1
"""
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse

from rest_framework.test import APIRequestFactory, force_authenticate

from accounts.models import Role
from ai_api.views import health_check
from work_record.health import probe_health

User = get_user_model()

PWD = "X12345678"


def grant_perms(user, *codes):
    """为测试用户授予 RBAC 权限码（与 notifications/tests.py 同款写法）。"""
    from accounts.models import Permission

    role = Role.objects.create(code=f"test-role-{user.username}", name="测试角色")
    role.permissions.set(Permission.objects.filter(code__in=codes))
    user.rbac_roles.add(role)


class SystemHealthViewTests(TestCase):
    """页面访问权限：system.read 可访问，缺失 403。"""

    @classmethod
    def setUpTestData(cls):
        call_command("sync_rbac", verbosity=0)
        cls.admin = User.objects.create_user("health_admin", password=PWD)
        grant_perms(cls.admin, "system.read")
        cls.staff = User.objects.create_user("health_staff", password=PWD)
        Role.objects.create(code="nohealth", name="无系统权限", is_builtin=False)
        cls.staff.rbac_roles.add(Role.objects.get(code="nohealth"))

    def test_ok_with_system_read(self):
        c = Client()
        c.force_login(self.admin)
        resp = c.get(reverse("system_health"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "系统健康")
        self.assertContains(resp, "健康探测")
        # 非目标提示区文案在位
        self.assertContains(resp, "批 C")

    def test_forbidden_without_system_read(self):
        c = Client()
        c.force_login(self.staff)
        resp = c.get(reverse("system_health"))
        self.assertEqual(resp.status_code, 403)


class ProbeHealthTests(TestCase):
    """probe_health 的输出结构，以及与 health_check API 的一致性。"""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("probe_user", password=PWD)

    def test_probe_structure(self):
        checks, healthy = probe_health()
        self.assertEqual(set(checks.keys()), {"database", "migrations", "log_dir"})
        self.assertIsInstance(healthy, bool)
        self.assertEqual(healthy, checks["database"] == "ok" and checks["log_dir"] == "ok")

    def test_probe_matches_health_check(self):
        """对同一请求分别调用：health_check 的 checks 与 status 应与 probe_health 一致。"""
        factory = APIRequestFactory()
        request = factory.get("/api/health/")
        force_authenticate(request, user=self.user)
        response = health_check(request)

        checks, healthy = probe_health()
        self.assertEqual(response.data["checks"], checks)
        self.assertEqual(response.data["status"], "ok" if healthy else "degraded")
        self.assertEqual(response.status_code, 200 if healthy else 503)
