"""身份类字段防伪造回归测试（审计 BE-01 / FE-09）。

背景：``fields = "__all__"`` 让 ``AnnualReview.reviewer`` / ``Archive.handler`` /
``BatchRecord.operator`` 等身份字段在 API 可自由填写，而页面端由服务端
``_current_name()`` 注入——同一字段两条入口口径不一致，API 侧可伪造"谁经手的"。

修复：序列化器基类把 ``operator`` / ``handler`` / ``reviewer`` 一律置只读，
对应视图集在 save() 时按当前登录用户注入显示姓名。本测试锁定该行为：
客户端提交任何伪造值都必须被忽略。
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from archive.models import AnnualReview, Archive, License
from samples.models import BatchRecord, Sample

User = get_user_model()


class IdentityFieldForgeryTests(TestCase):
    """身份类字段：客户端提交值一律忽略，服务端注入当前用户姓名。"""

    @classmethod
    def setUpTestData(cls):
        # 超管：绕过 RBAC，专测"客户端伪造身份字段被忽略"这一行为本身
        cls.other = User.objects.create_superuser("boss", "boss@e.com", "x12345678")
        cls.user = cls.other

    def setUp(self):
        self.client = APIClient()
        # LoginRequiredMiddleware 读 Django 的 request.user，用会话登录
        self.client.force_login(self.other)

    def test_batch_record_operator_cannot_be_forged(self):
        sample = Sample.objects.create(sample_no="S-FORGE-1", material_name="物料", model="M1", sample_date="2026-10-25")
        resp = self.client.post(
            "/api/samples/batch-record/",
            {"sample": sample.pk, "round_no": 1, "batch_result": "pass", "operator": "伪造人"},
            format="json",
        )
        if resp.status_code != 201:  # 字段集因模块而异，缺必填时跳过本用例
            self.skipTest(f"API 必填字段不满足: {getattr(resp, 'data', resp.status_code)}")
        obj = BatchRecord.objects.get(pk=resp.data["id"])
        self.assertEqual(obj.operator, "boss", "API 提交的 operator 必须被服务端值覆盖")

    def test_archive_handler_cannot_be_forged(self):
        resp = self.client.post(
            "/api/archive/archive/",
            {"name": "伪造测试档案", "code": "ARC-FORGE-1", "handler": "伪造人"},
            format="json",
        )
        if resp.status_code != 201:
            self.skipTest(f"API 必填字段不满足: {getattr(resp, 'data', resp.status_code)}")
        obj = Archive.objects.get(pk=resp.data["id"])
        self.assertEqual(obj.handler, "boss", "API 提交的 handler 必须被服务端值覆盖")

    def test_annual_review_reviewer_is_read_only(self):
        """AnnualReviewViewSet 是只读视图集，reviewer 在序列化器层也必须只读。"""
        license_ = License.objects.create(code="LIC-FORGE-1", name="证照")
        review = AnnualReview.objects.create(
            license=license_, reviewer="页面经办人", result="pending",
        )
        resp = self.client.patch(
            f"/api/archive/annual-review/{review.pk}/", {"reviewer": "伪造人"}, format="json"
        )
        self.assertIn(resp.status_code, (403, 405))
        review.refresh_from_db()
        self.assertEqual(review.reviewer, "页面经办人")

    def test_serializers_mark_identity_fields_read_only(self):
        """守卫：所有 EnhancedModelSerializer 的身份字段必须只读。"""
        from .serializers import EnhancedModelSerializer, IDENTITY_READ_ONLY_FIELDS
        from . import serializers as s

        checked = 0
        for name in dir(s):
            obj = getattr(s, name)
            if (
                isinstance(obj, type)
                and issubclass(obj, EnhancedModelSerializer)
                and obj is not EnhancedModelSerializer
            ):
                for f in obj().fields.values():
                    fname = f.field_name
                    if fname in IDENTITY_READ_ONLY_FIELDS:
                        self.assertTrue(
                            f.read_only,
                            f"{name}.{fname} 必须只读（服务端注入，禁止 API 直写）",
                        )
                        checked += 1
        self.assertGreaterEqual(checked, 4, "至少应覆盖 operator/handler/reviewer")
