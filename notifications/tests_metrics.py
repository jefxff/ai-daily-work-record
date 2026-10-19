"""请求性能采集（批次 C）测试：中间件落库 / 噪声路径跳过 / 探针匿名可达 / 性能页权限 / 采集工具单测。

运行：python manage.py test notifications.tests_metrics -v 1
"""
import time
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Permission, Role

from . import metrics
from .models import RequestMetric

User = get_user_model()

PWD = "X12345678"


def grant_perms(user, *codes):
    """为测试用户授予 RBAC 权限码（与 notifications/tests_health.py 同款写法）。"""
    role = Role.objects.create(code=f"test-role-{user.username}", name="测试角色")
    role.permissions.set(Permission.objects.filter(code__in=codes))
    user.rbac_roles.add(role)


class MetricsHelperTests(TestCase):
    """percentile / should_skip / make_query_collector 纯函数单测。"""

    def test_percentile_nearest_rank(self):
        values = list(range(1, 101))
        self.assertEqual(metrics.percentile(values, 95), 95)
        self.assertEqual(metrics.percentile(values, 50), 50)
        self.assertEqual(metrics.percentile([7, 3, 9], 100), 9)
        self.assertIsNone(metrics.percentile([], 95))

    def test_should_skip_noise_paths(self):
        for path in ("/static/css/app.css", "/media/x.png", "/favicon.ico", "/probe/", ""):
            self.assertTrue(metrics.should_skip(path), path)
        for path in ("/", "/system/perf/", "/samples/", "/accounts/login/"):
            self.assertFalse(metrics.should_skip(path), path)

    def test_query_collector_counts_and_flags_slow(self):
        state = {"db_count": 0, "db_time_ms": 0.0, "slow_query": ""}
        with override_settings(REQUEST_METRICS_SLOW_QUERY_MS=0):
            wrapper = metrics.make_query_collector(state)
            for _ in range(3):
                wrapper(lambda sql, params, many, context: None, "SELECT 1", (), False, {})
        self.assertEqual(state["db_count"], 3)
        self.assertGreaterEqual(state["db_time_ms"], 0)
        self.assertIn("SELECT 1", state["slow_query"])

    def test_is_enabled_toggle(self):
        self.assertTrue(metrics.is_enabled())
        with override_settings(REQUEST_METRICS_ENABLED=False):
            self.assertFalse(metrics.is_enabled())


class MetricsBufferTests(TestCase):
    """内存缓冲 + 阈值批量落库 + 旧数据裁剪。"""

    def setUp(self):
        metrics.reset()

    def tearDown(self):
        metrics.reset()

    def test_record_then_flush_persists(self):
        metrics.record(method="GET", path="/x/", status_code=200, duration_ms=12,
                       db_count=3, db_time_ms=5, slow_query="[250ms] SELECT 1", user="u1")
        # 未达落库阈值：仍在内存缓冲，库里没有
        self.assertEqual(RequestMetric.objects.count(), 0)
        metrics.flush()
        self.assertEqual(RequestMetric.objects.count(), 1)
        row = RequestMetric.objects.get()
        self.assertEqual((row.method, row.path, row.status_code, row.duration_ms), ("GET", "/x/", 200, 12))
        self.assertEqual((row.db_count, row.db_time_ms, row.user), (3, 5, "u1"))
        self.assertIn("SELECT 1", row.slow_query)

    def test_flush_empty_is_noop(self):
        metrics.flush()
        self.assertEqual(RequestMetric.objects.count(), 0)

    def test_flush_batch_drops_nothing_on_success(self):
        for i in range(10):
            metrics.record(method="GET", path=f"/p/{i}/", status_code=200, duration_ms=i)
        metrics.flush()
        self.assertEqual(RequestMetric.objects.count(), 10)

    def test_cleanup_prunes_expired_rows(self):
        RequestMetric.objects.create(method="GET", path="/old/", status_code=200, duration_ms=1)
        RequestMetric.objects.filter(path="/old/").update(
            created_at=timezone.now() - timedelta(days=60)
        )
        metrics._last_cleanup = time.monotonic() - metrics._CLEANUP_INTERVAL - 1  # 强制触发一次裁剪
        metrics._maybe_cleanup()
        self.assertFalse(RequestMetric.objects.filter(path="/old/").exists())

    def test_is_slow_badge(self):
        row = RequestMetric.objects.create(method="GET", path="/s/", status_code=200, duration_ms=1500)
        self.assertTrue(row.is_slow)
        quick = RequestMetric.objects.create(method="GET", path="/q/", status_code=200, duration_ms=5)
        self.assertFalse(quick.is_slow)


class RequestMetricsMiddlewareTests(TestCase):
    """中间件：业务请求落一行指标；噪声路径 / 关闭开关时不采集。"""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("metrics_user", password=PWD)

    def setUp(self):
        metrics.reset()

    def tearDown(self):
        metrics.reset()

    def test_records_business_request(self):
        c = Client()
        c.force_login(self.user)
        resp = c.get(reverse("home"))
        self.assertEqual(resp.status_code, 200)
        metrics.flush()
        row = RequestMetric.objects.filter(path=reverse("home")).order_by("-id").first()
        self.assertIsNotNone(row)
        self.assertEqual(row.method, "GET")
        self.assertEqual(row.status_code, 200)
        self.assertEqual(row.user, "metrics_user")
        self.assertGreaterEqual(row.db_count, 1)

    def test_skips_probe_path(self):
        c = Client()
        c.get(reverse("probe"))
        metrics.flush()
        self.assertFalse(RequestMetric.objects.filter(path__startswith="/probe/").exists())

    def test_disabled_records_nothing(self):
        with override_settings(REQUEST_METRICS_ENABLED=False):
            c = Client()
            c.force_login(self.user)
            c.get(reverse("home"))
        metrics.flush()
        self.assertEqual(RequestMetric.objects.count(), 0)


class ProbeViewTests(TestCase):
    """外部独立探针：免登录可达、不访问数据库、不泄露敏感信息。"""

    def setUp(self):
        metrics.reset()

    def tearDown(self):
        metrics.reset()

    def test_probe_anonymous_ok(self):
        c = Client()
        resp = c.get(reverse("probe"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/json")
        self.assertEqual(resp["Cache-Control"], "no-store")
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["service"], "daily-work-record")
        self.assertIn("timestamp", data)
        self.assertIn("uptime_seconds", data)
        # 不泄露版本 / 配置等敏感信息
        for leak in ("version", "settings", "secret", "db"):
            self.assertNotIn(leak, data)


class PerfDashboardViewTests(TestCase):
    """性能监控页：system.read 可访问，缺失 403；摘要与分位统计正确。"""

    @classmethod
    def setUpTestData(cls):
        call_command("sync_rbac", verbosity=0)
        cls.admin = User.objects.create_user("perf_admin", password=PWD)
        grant_perms(cls.admin, "system.read")
        cls.staff = User.objects.create_user("perf_staff", password=PWD)
        Role.objects.create(code="noperf", name="无系统权限", is_builtin=False)
        cls.staff.rbac_roles.add(Role.objects.get(code="noperf"))

    def setUp(self):
        metrics.reset()

    def tearDown(self):
        metrics.reset()

    def test_forbidden_without_system_read(self):
        c = Client()
        c.force_login(self.staff)
        self.assertEqual(c.get(reverse("perf_dashboard")).status_code, 403)

    def test_ok_with_system_read_and_summary(self):
        for i, duration in enumerate((10, 20, 30)):
            RequestMetric.objects.create(
                method="GET", path=f"/endpoint/{i}/", status_code=200, duration_ms=duration,
            )
        RequestMetric.objects.create(
            method="POST", path="/slow/", status_code=500, duration_ms=5000,
            slow_query="[900ms] SELECT * FROM t",
        )
        c = Client()
        c.force_login(self.admin)
        resp = c.get(reverse("perf_dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "性能监控")
        summary = resp.context["summary"]
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["p95_ms"], 5000)
        self.assertEqual(summary["error_count"], 1)
        self.assertEqual(summary["slow_query_count"], 1)
        # 榜单与慢查询列表均已渲染
        self.assertTrue(resp.context["top_paths"])
        self.assertEqual(len(resp.context["recent_slow_queries"]), 1)

    def test_days_clamped(self):
        c = Client()
        c.force_login(self.admin)
        self.assertEqual(c.get(reverse("perf_dashboard"), {"days": "0"}).context["days"], 1)
        self.assertEqual(c.get(reverse("perf_dashboard"), {"days": "999"}).context["days"], 90)
        self.assertEqual(c.get(reverse("perf_dashboard"), {"days": "abc"}).context["days"], 7)
