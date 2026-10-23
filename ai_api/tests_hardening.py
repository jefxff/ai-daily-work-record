"""ai_api 的改造验收测试（对应技术审计报告 v4 的 #8 / #9 / #23）。

覆盖三件事：
- #8：AI API 是"静默写通道"，创建/更新/删除都必须留痕，且操作类型映射
      不能把"新增"记成"删除"（samples 留痕已统一写入 records.OperationLog）。
- #9：序列化器不再 fields='__all__' 即"能写所有字段"，系统维护字段必须只读，
      否则 status / is_deleted 可以绕过状态机与软删除流程。
- #23：健康检查必须做真实探测，而不是恒定返回 ok。
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from rest_framework.test import APIClient

from records.models import DailyRecord, OperationLog, Regulation

from .serializers import (
    DailyRecordSerializer,
    RegulationSerializer,
    SampleSerializer,
)
from .views import SampleViewSet

User = get_user_model()


class AuditTrailTests(TestCase):
    """#8：创建/更新/删除三条写路径都要落留痕。"""

    def setUp(self):
        self.root = User.objects.create_superuser("api_root", "api@e.com", "x12345678")
        self.client = APIClient()
        # 用会话登录而不是 force_authenticate：项目有 LoginRequiredMiddleware，
        # 它读的是 Django 的 request.user，force_authenticate 不会设置该属性。
        self.client.force_login(self.root)
        self.payload = {
            "issue_date": "2026-01-05",
            "title": "考勤制度",
            "content": "上下班打卡",
            "doc_no": "ZD-001",
        }

    def _last_log(self):
        return OperationLog.objects.filter(module_type="制度及处罚").order_by("-id").first()

    def test_create_writes_audit_log(self):
        resp = self.client.post("/api/regulation/", self.payload, format="json")
        self.assertEqual(resp.status_code, 201)
        log = self._last_log()
        self.assertIsNotNone(log, "创建后必须留痕（改造前只在 update/destroy 留痕）")
        self.assertEqual(log.operation_type, "新增")
        self.assertEqual(log.operator, "api_root")
        self.assertIn(f"记录#{resp.data['id']}", log.object_identifier)

    def test_update_writes_audit_log(self):
        obj = Regulation.objects.create(**{**self.payload, "issue_date": date(2026, 1, 5)})
        resp = self.client.patch(
            f"/api/regulation/{obj.pk}/", {"title": "考勤制度（修订）"}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        log = self._last_log()
        self.assertEqual(log.operation_type, "更新")
        self.assertIn("考勤制度（修订）", log.change_summary)

    def test_delete_writes_audit_log(self):
        obj = Regulation.objects.create(**{**self.payload, "issue_date": date(2026, 1, 5)})
        pk = obj.pk
        resp = self.client.delete(f"/api/regulation/{obj.pk}/")
        self.assertEqual(resp.status_code, 204)
        log = self._last_log()
        self.assertEqual(log.operation_type, "删除")
        # delete() 会把实例 pk 置 None，留痕必须用删除前取到的 id
        self.assertIn(f"记录#{pk}", log.object_identifier)

    def test_samples_audit_action_mapping(self):
        """samples 统一留痕的动作映射：'新增' 不能落成'删除'（改造前三元表达式的坑）。"""
        request = RequestFactory().post("/api/samples/sample/")
        request.user = self.root
        view = SampleViewSet()
        view.request = request

        view._write_audit("新增", 1, "新增样品: S2601001")
        log = OperationLog.objects.filter(module_type="试样管理").order_by("-id").first()
        self.assertEqual(log.operation_type, "新增")
        self.assertEqual(log.object_identifier, "样品主档#1")

        view._write_audit("更新", 1, "编辑样品: S2601001")
        log = OperationLog.objects.filter(module_type="试样管理").order_by("-id").first()
        self.assertEqual(log.operation_type, "更新")

        view._write_audit("删除", 1, "删除样品: S2601001")
        log = OperationLog.objects.filter(module_type="试样管理").order_by("-id").first()
        self.assertEqual(log.operation_type, "删除")


class SerializerReadOnlyTests(TestCase):
    """#9：系统维护字段只读，业务状态字段由状态机独占。"""

    def test_system_fields_are_read_only(self):
        fields = RegulationSerializer().get_fields()
        self.assertTrue(fields["id"].read_only)

    def test_sample_status_and_no_are_read_only(self):
        fields = SampleSerializer().get_fields()
        for name in ("status", "sample_no", "is_deleted", "deleted_at", "deleted_by"):
            self.assertTrue(fields[name].read_only, f"{name} 必须只读")

    def test_required_business_fields_stay_writable(self):
        """收窄只针对系统字段，业务必填字段不能被误伤。"""
        fields = SampleSerializer().get_fields()
        for name in ("material_name", "model", "sample_date"):
            self.assertFalse(fields[name].read_only, f"{name} 不应被置为只读")

    def test_api_cannot_bypass_soft_delete_flag(self):
        """POST 带 is_deleted=true 时不能把新记录直接标成已删除。"""
        from samples.models import Sample

        root = User.objects.create_superuser("api_root2", "api2@e.com", "x12345678")
        client = APIClient()
        client.force_login(root)
        resp = client.post(
            "/api/samples/sample/",
            {
                "material_name": "铜排",
                "model": "TMY-01",
                "sample_date": "2026-01-06",
                "is_deleted": True,
                "status": "imported",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        sample = Sample.objects.get(pk=resp.data["id"])
        self.assertFalse(sample.is_deleted, "is_deleted 必须由软删除流程控制")
        self.assertEqual(sample.status, Sample.Status.PENDING, "status 必须由状态机推进")


class AiCreateTests(TestCase):
    """ai_create 的归属绑定：DailyRecord.user 只读，必须由服务端兜住。"""

    def setUp(self):
        self.root = User.objects.create_superuser("ai_root", "ai@e.com", "x12345678")
        self.client = APIClient()
        # 用会话登录而不是 force_authenticate：项目有 LoginRequiredMiddleware，
        # 它读的是 Django 的 request.user，force_authenticate 不会设置该属性。
        self.client.force_login(self.root)

    def test_daily_record_binds_current_user(self):
        resp = self.client.post(
            "/api/ai/create/",
            {
                "model": "daily_record",
                "data": {
                    "department": "工程部",
                    "work_content": "跟进装修进度",
                    "status": "未完成",
                },
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        record = DailyRecord.objects.get(pk=resp.data["id"])
        self.assertEqual(record.user_id, self.root.pk)

    def test_invalid_model_type_is_400(self):
        resp = self.client.post(
            "/api/ai/create/", {"model": "not_a_model", "data": {}}, format="json"
        )
        self.assertEqual(resp.status_code, 400)


class HealthCheckTests(TestCase):
    """#23：健康检查做真实探测。"""

    def setUp(self):
        self.root = User.objects.create_superuser("hc_root", "hc@e.com", "x12345678")
        self.client = APIClient()

    def test_health_reports_real_checks(self):
        self.client.force_login(self.root)
        resp = self.client.get("/api/ai/health/")
        self.assertIn(resp.status_code, (200, 503))
        data = resp.json()
        self.assertIn("checks", data)
        self.assertEqual(data["checks"]["database"], "ok")
        self.assertEqual(data["checks"]["log_dir"], "ok")
        self.assertIn(data["status"], ("ok", "degraded"))
        # 旧键保留，避免既有调用方取值 KeyError
        self.assertIn("ai_ready", data)
        self.assertEqual(resp.status_code, 200 if data["status"] == "ok" else 503)

    def test_anonymous_is_rejected(self):
        resp = self.client.get("/api/ai/health/")
        self.assertIn(resp.status_code, (401, 403))


class RbacEnforcementTests(TestCase):
    """接口与页面同一把锁：未登录/无权限不得读写。"""

    def test_anonymous_list_rejected(self):
        resp = APIClient().get("/api/regulation/")
        self.assertIn(resp.status_code, (401, 403))

    def test_user_without_module_perm_is_forbidden(self):
        plain = User.objects.create_user("api_plain", "p@e.com", "x12345678")
        client = APIClient()
        client.force_login(plain)
        self.assertIn(client.get("/api/regulation/").status_code, (401, 403))


class AiSearchPermKeyTests(TestCase):
    """R6：ai_search 的权限模块 key 必须映射到 registry 中的真实模块。

    改造前直接用别名（partner / sample / test_record / batch_record / audit_record /
    license / cabinet / knowledge_document 等）去调 _can()，而这些 key 不在账户模块表中，
    _can() 恒为 False——拥有 samples 权限的用户搜索"供应商/样品"永远为空，
    接口对合法调用方静默返回空结果（且无法从响应分辨是"没数据"还是"没权限"）。
    修复后按模块归属显式映射（samples / archive / knowledge）。
    """

    @staticmethod
    def _grant(module, actions, user):
        from django.core.cache import cache

        from accounts.models import Permission, Role

        role, _ = Role.objects.get_or_create(
            code=f"api_t_{module}", defaults={"name": f"测试-{module}", "is_active": True}
        )
        for action in actions:
            perm, _ = Permission.objects.get_or_create(
                module=module,
                action=action,
                defaults={"code": f"{module}:{action}", "name": f"{module}-{action}"},
            )
            role.permissions.add(perm)
        role.users.add(user)
        if hasattr(user, "_rbac_codes_cache"):
            del user._rbac_codes_cache
        cache.clear()

    def setUp(self):
        from django.core.management import call_command

        call_command("sync_rbac", verbosity=0)
        from samples.models import Partner, Sample

        Partner.objects.create(partner_code="P-1", name="洪兴五金")
        Sample.objects.create(
            sample_no="S-X1", material_name="磷青铜", model="M-1", sample_date=date(2026, 1, 1)
        )
        self.samples_user = User.objects.create_user("ai_s_samples", "ss@e.com", "x12345678")
        self._grant("samples", ("read",), self.samples_user)

    def test_samples_perm_user_sees_partner(self):
        """partner 归 samples 模块：修复前恒不可见，修复后能命中。"""
        client = APIClient()
        client.force_login(self.samples_user)
        resp = client.get("/api/ai/search/", {"q": "洪兴"})
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()
        self.assertIn("partner", data, "拥有 samples 权限的用户应能搜到供应商档案")
        self.assertEqual(data["partner"][0]["name"], "洪兴五金")

    def test_no_perm_user_sees_nothing(self):
        plain = User.objects.create_user("ai_s_plain", "sp@e.com", "x12345678")
        client = APIClient()
        client.force_login(plain)
        resp = client.get("/api/ai/search/", {"q": "洪兴"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertNotIn("partner", data)
        self.assertNotIn("sample", data)

    def test_archive_perm_does_not_leak_samples(self):
        """权限隔离：archive 权限用户不得通过搜索越权读到 samples 模块数据。"""
        archive_user = User.objects.create_user("ai_s_arch", "sa@e.com", "x12345678")
        self._grant("archive", ("read",), archive_user)
        client = APIClient()
        client.force_login(archive_user)
        resp = client.get("/api/ai/search/", {"q": "洪兴"})
        data = resp.json()
        self.assertNotIn("partner", data)
        self.assertNotIn("sample", data)

    def test_explicit_model_without_perm_is_403(self):
        """指定 model=partner 但无 samples 权限时返回 403，不得借搜索通道越权。"""
        plain = User.objects.create_user("ai_s_plain2", "sp2@e.com", "x12345678")
        client = APIClient()
        client.force_login(plain)
        resp = client.get("/api/ai/search/", {"q": "洪兴", "model": "partner"})
        self.assertEqual(resp.status_code, 403)


class AiConfigBaseUrlTests(TestCase):
    """审计 SEC-07：管理员可控的 base_url 必须挡住 SSRF 高危目标。"""

    def test_validate_rejects_non_http_scheme(self):
        from .views_config import _validate_base_url

        self.assertIn("http://", _validate_base_url("file:///etc/passwd"))
        self.assertIn("http://", _validate_base_url("ftp://example.com"))

    def test_validate_rejects_link_local_metadata(self):
        from .views_config import _validate_base_url

        self.assertTrue(_validate_base_url("http://169.254.169.254/latest/meta-data"))
        self.assertIn("保留", _validate_base_url("http://metadata.google.internal/"))

    def test_validate_allows_public_and_lan_hosts(self):
        from .views_config import _validate_base_url

        self.assertEqual(_validate_base_url("https://api.minimax.chat/v1"), "")
        self.assertEqual(_validate_base_url("http://127.0.0.1:11434"), "")
        self.assertEqual(_validate_base_url("http://192.168.1.20:11434"), "")

    def test_test_endpoint_returns_error_for_metadata_url(self):
        from unittest import mock

        from django.urls import reverse

        root = User.objects.create_superuser("cfg_root", "cfg@e.com", "x12345678")
        client = APIClient()
        client.force_login(root)
        with mock.patch("ai_api.views_config.test_connection") as conn:
            resp = client.post(
                reverse("ai_config_test"),
                {"base_url": "http://169.254.169.254/latest", "provider": "ollama"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["ok"])
        conn.assert_not_called()
