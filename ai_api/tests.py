"""AI API 接口测试：重点覆盖搜索通道的数据隔离（审计 P1-1）。"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from records.models import DailyRecord

User = get_user_model()


class AISearchOwnerIsolationTests(TestCase):
    """daily_record 必须按 user 过滤，禁止借 AI 搜索读取他人记录。"""

    def setUp(self):
        self.owner = User.objects.create_superuser("ai_owner", "o@e.com", "x12345678")
        self.viewer = User.objects.create_superuser("ai_viewer", "v@e.com", "x12345678")
        self.secret_text = "绝密项目甲代号ZX-9"
        DailyRecord.objects.create(
            user=self.owner, department="研发部",
            work_content=self.secret_text, status="未完成",
        )

    def test_specified_model_is_owner_scoped(self):
        self.client.force_login(self.viewer)
        resp = self.client.get("/api/ai/search/", {"q": "代号ZX-9", "model": "daily_record"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("daily_record", []), [],
                         "不得检索到其他用户的每日记录")

    def test_global_search_is_owner_scoped(self):
        self.client.force_login(self.viewer)
        resp = self.client.get("/api/ai/search/", {"q": "代号ZX-9"})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("daily_record", resp.json(),
                         "全局搜索也不得返回其他用户的每日记录")

    def test_owner_can_find_own_record(self):
        self.client.force_login(self.owner)
        resp = self.client.get("/api/ai/search/", {"q": "代号ZX-9", "model": "daily_record"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json().get("daily_record", [])), 1)


class SerializerFieldContractTests(TestCase):
    """BE-21：序列化器字段白名单契约。

    新增模型字段时必须同步这里的期望集合，否则测试失败——
    防止「模型加字段 → API 自动暴露」的静默扩张。
    """

    EXPECTED_DAILY_RECORD_FIELDS = {
        "id", "user", "record_date", "department", "work_content", "due_date",
        "status", "progress", "kb_enabled", "kb_visibility", "kb_doc", "kb_synced_at",
    }

    # 任何序列化器都不允许可写的敏感字段（新增字段命中即失败）
    SENSITIVE_PREFIXES = ("password", "secret", "token", "private_key", "id_card")

    def test_daily_record_serializer_exposes_only_expected_fields(self):
        from ai_api.serializers import DailyRecordSerializer

        self.assertEqual(set(DailyRecordSerializer().fields), self.EXPECTED_DAILY_RECORD_FIELDS)

    def test_no_serializer_exposes_writable_sensitive_field(self):
        from ai_api import serializers as serializers_module

        offenders = []
        for name in dir(serializers_module):
            cls = getattr(serializers_module, name)
            if not isinstance(cls, type) or not issubclass(cls, serializers_module.EnhancedModelSerializer) \
                    or cls is serializers_module.EnhancedModelSerializer:
                continue
            try:
                serializer = cls()
            except Exception:
                continue
            for field_name, field in serializer.fields.items():
                if field_name.startswith(self.SENSITIVE_PREFIXES) and not field.read_only:
                    offenders.append(f"{name}.{field_name}")
        self.assertEqual(offenders, [], f"敏感字段必须只读：{offenders}")


class UnifiedErrorStructureTests(TestCase):
    """X-05：DRF 错误响应统一包含 code/message/field_errors。"""

    REQUIRED_KEYS = ("code", "message", "field_errors", "detail", "error")

    def test_unauthorized_api_error_is_unified(self):
        resp = self.client.get("/api/daily-record/1/")
        self.assertIn(resp.status_code, (401, 403))
        data = resp.json()
        for key in self.REQUIRED_KEYS:
            self.assertIn(key, data)

    def test_validation_error_is_unified_and_keeps_field_errors(self):
        user = User.objects.create_superuser("api_root", "a@e.com", "x12345678")
        self.client.force_login(user)
        resp = self.client.post("/api/decoration/", {})
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        for key in self.REQUIRED_KEYS:
            self.assertIn(key, data)
        self.assertIsInstance(data["field_errors"], dict)
        self.assertTrue(data["field_errors"])
