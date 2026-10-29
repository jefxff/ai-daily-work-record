"""日志中心验收测试。

覆盖：权限（匿名 / 无权限 / 管理员）、页面渲染与筛选分页、非法参数、XSS 转义、
增量采集（续行堆栈 / 轮转 / 跨文件去重 / 敏感掩码 / 访问流水过滤 / 保留裁剪）、
手动采集、CSV 导出（公式注入防护）与 API 契约。
"""
import os
import shutil
import tempfile
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import caches
from django.core.management import call_command
from django.core.paginator import Page
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Role
from records.models import OperationLog

from . import logs_service
from .models import SystemLog

PWD = "X12345678"


def _log_line(dt, level, logger_name, message):
    return f"{dt.strftime('%Y-%m-%d %H:%M:%S,')}{dt.microsecond // 1000:03d} {level} {logger_name} {message}"


class LogCenterPermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("sync_rbac", verbosity=0)
        cls.admin = User.objects.create_user("admin_log", "a@e.com", PWD)
        cls.admin.rbac_roles.add(Role.objects.get(code="admin"))
        cls.staff = User.objects.create_user("staff_log", "s@e.com", PWD)
        cls.staff.rbac_roles.add(Role.objects.get(code="staff"))

    def setUp(self):
        caches["fast"].clear()

    def tearDown(self):
        caches["fast"].clear()

    def test_anonymous_redirects(self):
        response = self.client.get(reverse("system_logs"))
        self.assertEqual(response.status_code, 302)

    def test_staff_forbidden(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("system_logs")).status_code, 403)

    def test_admin_sees_sidebar_entry(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("system_logs"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "日志中心")
        html = self.client.get(reverse("home")).content.decode()
        self.assertIn("日志中心", html)

    def test_ingest_requires_update_and_post(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse("system_logs_ingest"),
                                    HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 403)

        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.get(reverse("system_logs_ingest"),
                            HTTP_X_REQUESTED_WITH="XMLHttpRequest").status_code, 405
        )


class LogCenterPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("sync_rbac", verbosity=0)
        cls.admin = User.objects.create_user("admin_page", "a@e.com", PWD)
        cls.admin.rbac_roles.add(Role.objects.get(code="admin"))

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.admin)
        caches["fast"].clear()
        self.tmpdir = tempfile.mkdtemp(prefix="logcenter_")
        os.makedirs(os.path.join(self.tmpdir, "logs"))
        self.override = override_settings(
            BASE_DIR=self.tmpdir,
            LOG_INGEST_ENABLED=False,
        )
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        caches["fast"].clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make(self, message, level="INFO", source="debug.log", logger_name="app",
              minutes_ago=0):
        return SystemLog.objects.create(
            logged_at=timezone.now() - timedelta(minutes=minutes_ago),
            level=level,
            logger=logger_name,
            source=source,
            message=message,
            fingerprint=f"fp-{SystemLog.objects.count()}-{message[:20]}",
        )

    def test_filters_and_pagination(self):
        self._make("普通信息")
        self._make("错误详情", level="ERROR", logger_name="app.job")
        self._make("旧日志", minutes_ago=60 * 24 * 10)
        self._make("其他文件", source="csp_violations.log")

        response = self.client.get(reverse("system_logs"))
        page_obj = response.context["logs"]
        self.assertIsInstance(page_obj, Page)
        # 默认 7 天窗口：3 条近期记录，10 天前的旧记录不计入
        self.assertEqual(page_obj.paginator.count, 3)

        response = self.client.get(reverse("system_logs"), {"level": "ERROR"})
        self.assertEqual(response.context["logs"].paginator.count, 1)

        response = self.client.get(reverse("system_logs"), {"source": "csp_violations.log"})
        self.assertEqual(response.context["logs"].paginator.count, 1)

        response = self.client.get(reverse("system_logs"), {"search": "旧日志"})
        self.assertEqual(response.context["logs"].paginator.count, 0)  # 默认 7 天窗口

        response = self.client.get(reverse("system_logs"), {"search": "旧日志", "days": 0})
        self.assertEqual(response.context["logs"].paginator.count, 1)

        for i in range(25):
            self._make(f"批量{i}")
        response = self.client.get(reverse("system_logs"), {"days": 0, "per_page": 30})
        self.assertEqual(response.context["logs"].paginator.per_page, 30)
        self.assertEqual(response.context["logs"].paginator.count, 29)

    def test_invalid_filters_return_400(self):
        self.assertEqual(
            self.client.get(reverse("system_logs"), {"level": "TRACE"}).status_code, 400
        )
        self.assertEqual(
            self.client.get(reverse("system_logs"), {"source": "../secret.log"}).status_code, 400
        )
        self.assertEqual(
            self.client.get(reverse("system_logs_export"), {"level": "TRACE"}).status_code, 400
        )

    def test_message_is_escaped(self):
        self._make("<script>alert(1)</script>")
        response = self.client.get(reverse("system_logs"))
        content = response.content.decode()
        self.assertNotIn("<script>alert(1)</script>", content)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", content)

    def test_export_csv_and_audit(self):
        self._make("=1+1 公式注入测试", level="ERROR")
        before = OperationLog.objects.count()
        response = self.client.get(reverse("system_logs_export"), {"level": "ERROR"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        body = response.content.decode("utf-8-sig")
        self.assertIn("时间,级别,来源,文件,内容", body)
        # 以 = 开头的文本被前置单引号，避免被 Excel 当公式执行
        self.assertIn("'=1+1", body)
        self.assertGreaterEqual(OperationLog.objects.count(), before + 1)

    def test_stats_and_files_panel(self):
        self._make("错误", level="ERROR")
        response = self.client.get(reverse("system_logs"))
        self.assertEqual(response.context["stats"]["errors"], 1)
        self.assertEqual(response.context["files"], [])


class LogIngestTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("sync_rbac", verbosity=0)
        cls.admin = User.objects.create_user("admin_ingest", "a@e.com", PWD)
        cls.admin.rbac_roles.add(Role.objects.get(code="admin"))

    def setUp(self):
        caches["fast"].clear()
        self.tmpdir = tempfile.mkdtemp(prefix="logingest_")
        self.log_dir = os.path.join(self.tmpdir, "logs")
        os.makedirs(self.log_dir)
        self.override = override_settings(
            BASE_DIR=self.tmpdir,
            LOG_INGEST_ENABLED=True,
            LOG_INGEST_FILES=("debug.log*", "csp_violations.log"),
            LOG_INGEST_MAX_BYTES=1024 * 1024,
            LOG_MASK_SENSITIVE=True,
            LOG_RETENTION_DAYS=30,
            LOG_MAX_ROWS=100000,
            LOG_INGEST_SKIP_LOGGERS=("django.server",),
        )
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        caches["fast"].clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write(self, name, content, mode="w"):
        path = os.path.join(self.log_dir, name)
        with open(path, mode, encoding="utf-8") as fh:
            fh.write(content)
        return path

    def test_incremental_parse_and_traceback_merge(self):
        now = timezone.localtime()
        content = "\n".join([
            _log_line(now, "INFO", "app.start", "启动完成"),
            _log_line(now, "ERROR", "app.job", "任务失败"),
            'Traceback (most recent call last):',
            '  File "x.py", line 1',
            "ValueError: boom",
            "",
        ])
        self._write("debug.log", content)

        result = logs_service.ingest_logs()
        self.assertEqual(result["ingested"], 2)
        self.assertEqual(SystemLog.objects.count(), 2)
        error = SystemLog.objects.get(level="ERROR")
        self.assertIn("Traceback", error.message)
        self.assertIn("ValueError: boom", error.message)

        self.assertEqual(logs_service.ingest_logs()["parsed"], 0)

        self._write("debug.log", _log_line(now, "WARNING", "app.job", "新警告") + "\n",
                    mode="a")
        self.assertEqual(logs_service.ingest_logs()["ingested"], 1)

    def test_cross_file_dedupe(self):
        now = timezone.localtime()
        content = _log_line(now, "INFO", "app", "同一行") + "\n"
        self._write("debug.log", content)
        self.assertEqual(logs_service.ingest_logs()["ingested"], 1)
        # 轮转后内容进入归档文件，指纹相同 → 不重复入库
        self._write("debug.log.2", content)
        result = logs_service.ingest_logs()
        self.assertEqual(result["parsed"], 1)
        self.assertEqual(result["ingested"], 0)
        self.assertEqual(SystemLog.objects.count(), 1)

    def test_rotation_reset_offset(self):
        now = timezone.localtime()
        self._write("debug.log", "\n".join([
            _log_line(now, "INFO", "app", "A"),
            _log_line(now, "INFO", "app", "B"),
            "",
        ]))
        self.assertEqual(logs_service.ingest_logs()["ingested"], 2)
        # 文件被截断重写且比原偏移更短：偏移归零重读，靠指纹去重
        self._write("debug.log", _log_line(now, "INFO", "app", "C") + "\n")
        result = logs_service.ingest_logs()
        self.assertEqual(result["ingested"], 1)
        self.assertEqual(SystemLog.objects.count(), 3)

    def test_skip_access_logger_and_mask_sensitive(self):
        now = timezone.localtime()
        content = "\n".join([
            _log_line(now, "INFO", "django.server", '"GET / HTTP/1.1" 200'),
            _log_line(now, "ERROR", "app.job", "调用失败 password=abc123 token=xyz"),
            "",
        ])
        self._write("debug.log", content)
        logs_service.ingest_logs()

        self.assertEqual(SystemLog.objects.count(), 1)
        message = SystemLog.objects.get().message
        self.assertNotIn("abc123", message)
        self.assertNotIn("xyz", message)
        self.assertIn("password=***", message)

    def test_retention_and_row_cap(self):
        old = SystemLog.objects.create(
            logged_at=timezone.now() - timedelta(days=40),
            level="INFO", logger="old", source="debug.log", message="旧",
            fingerprint="old-fp",
        )
        now = timezone.localtime()
        self._write("debug.log", _log_line(now, "INFO", "app", "触发裁剪") + "\n")
        logs_service.ingest_logs()
        self.assertFalse(SystemLog.objects.filter(pk=old.pk).exists())

        with override_settings(LOG_MAX_ROWS=3):
            for i in range(6):
                SystemLog.objects.create(
                    logged_at=timezone.now() - timedelta(minutes=i),
                    level="INFO", logger="cap", source="debug.log", message=f"超额{i}",
                    fingerprint=f"cap-{i}",
                )
            logs_service.cleanup_logs()
        self.assertEqual(SystemLog.objects.count(), 3)

    def test_manual_ingest_endpoint_and_lazy_ingest(self):
        now = timezone.localtime()
        self._write("debug.log", _log_line(now, "INFO", "app", "手动采集") + "\n")
        self.client.force_login(self.admin)
        response = self.client.post(reverse("system_logs_ingest"),
                                    HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(SystemLog.objects.count(), 1)

        # 页面懒采集：清掉节流标记后打开页面会再次采集新增内容
        caches["fast"].clear()
        self._write("debug.log", _log_line(now, "ERROR", "app", "页面懒采集") + "\n",
                    mode="a")
        self.client.get(reverse("system_logs"))
        self.assertEqual(SystemLog.objects.count(), 2)


class SystemLogsApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("sync_rbac", verbosity=0)
        cls.admin = User.objects.create_user("admin_api_log", "a@e.com", PWD)
        cls.admin.rbac_roles.add(Role.objects.get(code="admin"))
        cls.staff = User.objects.create_user("staff_api_log", "s@e.com", PWD)
        cls.staff.rbac_roles.add(Role.objects.get(code="staff"))

    def setUp(self):
        caches["fast"].clear()

    def tearDown(self):
        caches["fast"].clear()

    def test_api_permission_and_contract(self):
        SystemLog.objects.create(
            logged_at=timezone.now(), level="ERROR", logger="app",
            source="debug.log", message="错误", fingerprint="api-fp",
        )
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("api-system-logs")).status_code, 403)

        self.client.force_login(self.admin)
        response = self.client.get(reverse("api-system-logs"), {"level": "ERROR"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["items"][0]["level"], "ERROR")
        self.assertEqual(body["filters"]["level"], "ERROR")
        self.assertIn("stats", body)

        bad = self.client.get(reverse("api-system-logs"), {"level": "TRACE"})
        self.assertEqual(bad.status_code, 400)

    def test_api_anonymous_denied(self):
        response = self.client.get(reverse("api-system-logs"))
        self.assertIn(response.status_code, (401, 403))
