"""通用附件模块测试：校验器、上传/删除视图、权限、媒体路径映射、计数注入。"""

import os

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from records.models import Complaint, CustomerComplaint, Insurance

from .models import ATTACHMENT_CATEGORIES, Attachment
from .services import inject_counts, module_key_for, panel_context
from .validators import validate_attachment

User = get_user_model()

# 最小合法 PNG（1x1 透明像素）
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF"


def png(name="photo.png"):
    return SimpleUploadedFile(name, PNG_BYTES, content_type="image/png")


def pdf(name="doc.pdf"):
    return SimpleUploadedFile(name, PDF_BYTES, content_type="application/pdf")


class ValidatorTests(TestCase):
    """上传校验：扩展名白名单 + 大小上限 + magic byte。"""

    def test_valid_png_and_pdf_pass(self):
        self.assertEqual(validate_attachment(png()), "")
        self.assertEqual(validate_attachment(pdf()), "")

    def test_disallowed_extension_rejected(self):
        f = SimpleUploadedFile("evil.exe", b"MZ\x90\x00", content_type="application/octet-stream")
        self.assertIn("不支持的文件类型", validate_attachment(f))

    def test_magic_mismatch_rejected(self):
        """后缀 .pdf 但内容是明文 —— 必须被文件头校验拦下。"""
        f = SimpleUploadedFile("fake.pdf", b"this is not a pdf at all", content_type="application/pdf")
        self.assertEqual(validate_attachment(f), "文件内容与扩展名不符，已拒绝上传")

    def test_oversize_rejected(self):
        big = SimpleUploadedFile("big.png", PNG_BYTES + b"\x00" * (21 * 1024 * 1024), content_type="image/png")
        self.assertIn("超过上限", validate_attachment(big))


class ModelMappingTests(TestCase):
    """模型 → 模块 key 映射与分类标签。"""

    def test_registered_models(self):
        self.assertEqual(module_key_for(CustomerComplaint()), "customer_complaint")
        self.assertEqual(module_key_for(Complaint()), "complaint")
        self.assertEqual(module_key_for(Insurance()), "insurance")

    def test_unregistered_model_returns_empty(self):
        self.assertEqual(module_key_for(User()), "")

    def test_categories_defined_for_evidence_modules(self):
        self.assertTrue(ATTACHMENT_CATEGORIES["customer_complaint"])
        self.assertTrue(ATTACHMENT_CATEGORIES["complaint"])
        self.assertTrue(ATTACHMENT_CATEGORIES["insurance"])


@override_settings(MEDIA_ROOT=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "test_media"))
class UploadViewTests(TestCase):
    """上传/删除视图：成功路径、逐个校验、权限、404。"""

    def setUp(self):
        self.user = User.objects.create_user("u1", password="x")
        self.complaint = CustomerComplaint.objects.create(
            customer_name="测试客户", responsible_salesperson="小张", complaint_date="2026-01-01"
        )

    def tearDown(self):
        # override_settings 把 MEDIA_ROOT 指到了测试目录，跑完必须清掉，
        # 否则仓库里会残留测试产生的媒体文件
        import shutil

        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)

    def _url(self):
        return reverse(
            "attachments:upload",
            args=["records", "customercomplaint", self.complaint.pk],
        )

    def test_upload_requires_login(self):
        resp = self.client.post(
            self._url(), {"files": [png()]}, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        # 匿名请求先被 LoginRequiredMiddleware 拦截（302 跳登录页）
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Attachment.objects.count(), 0)

    def test_upload_ok_creates_attachment_and_file(self):
        self.user.is_superuser = True
        self.user.save()
        self.client.force_login(self.user)
        resp = self.client.post(
            self._url(),
            {"files": [png("a.png"), pdf("b.pdf")], "category": "accept"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["created"], 2)
        atts = Attachment.objects.all()
        self.assertEqual(atts.count(), 2)
        for att in atts:
            self.assertTrue(att.file.name.startswith("attachments/customer_complaint/"))
            self.assertTrue(os.path.exists(att.file.path))
            self.assertEqual(att.category, "accept")
            self.assertEqual(att.uploaded_by, self.user)

    def test_partial_failure_returns_error_list(self):
        self.user.is_superuser = True
        self.user.save()
        self.client.force_login(self.user)
        resp = self.client.post(
            self._url(),
            {"files": [png("ok.png"), SimpleUploadedFile("bad.exe", b"MZ")]},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["created"], 1)
        self.assertEqual(len(body["errors"]), 1)
        self.assertEqual(Attachment.objects.count(), 1)

    def test_all_failed_returns_422(self):
        self.user.is_superuser = True
        self.user.save()
        self.client.force_login(self.user)
        resp = self.client.post(
            self._url(),
            {"files": [SimpleUploadedFile("bad.exe", b"MZ")]},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(Attachment.objects.count(), 0)

    def test_invalid_category_falls_back_to_empty(self):
        self.user.is_superuser = True
        self.user.save()
        self.client.force_login(self.user)
        self.client.post(
            self._url(), {"files": [png()], "category": "not-a-real-category"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(Attachment.objects.get().category, "")

    def test_unregistered_model_404(self):
        self.user.is_superuser = True
        self.user.save()
        self.client.force_login(self.user)
        resp = self.client.post(
            reverse("attachments:upload", args=["auth", "user", self.user.pk]),
            {"files": [png()]},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(Attachment.objects.count(), 0)

    def test_missing_record_404(self):
        self.user.is_superuser = True
        self.user.save()
        self.client.force_login(self.user)
        resp = self.client.post(
            reverse("attachments:upload", args=["records", "customercomplaint", 99999]),
            {"files": [png()]},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 404)

    def test_permission_denied_without_module_update(self):
        """仅有 customer_complaint.read 的普通用户不能上传。"""
        from accounts.models import Permission as RbacPermission, Role

        role = Role.objects.create(code="readonly_cc", name="只读客诉")
        perm, _ = RbacPermission.objects.get_or_create(
            module="customer_complaint", action="read",
            defaults={"code": "customer_complaint.read", "name": "客诉查看"},
        )
        role.permissions.add(perm)
        role.users.add(self.user)
        self.client.force_login(self.user)
        resp = self.client.post(
            self._url(), {"files": [png()]}, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(Attachment.objects.count(), 0)

    def test_delete_removes_row_and_file(self):
        self.user.is_superuser = True
        self.user.save()
        self.client.force_login(self.user)
        self.client.post(self._url(), {"files": [png()]}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        att = Attachment.objects.get()
        path = att.file.path
        resp = self.client.post(
            reverse("attachments:delete", args=[att.pk]),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(Attachment.objects.count(), 0)
        self.assertFalse(os.path.exists(path))  # 物理文件一并删除


class ServicesTests(TestCase):
    """panel_context / inject_counts / 媒体路径模块映射。"""

    def test_inject_counts_single_query_batch(self):
        from django.contrib.contenttypes.models import ContentType as CT

        ct = CT.objects.get_for_model(CustomerComplaint)
        objs = [
            CustomerComplaint.objects.create(
                customer_name=f"客户{i}", responsible_salesperson="小张",
                complaint_date="2026-01-01",
            )
            for i in range(3)
        ]
        Attachment.objects.create(
            content_type=ct, object_id=objs[0].pk, file="attachments/customer_complaint/x/a.png",
            original_name="a.png", size=1,
        )
        Attachment.objects.create(
            content_type=ct, object_id=objs[0].pk, file="attachments/customer_complaint/x/b.png",
            original_name="b.png", size=1,
        )
        Attachment.objects.create(
            content_type=ct, object_id=objs[2].pk, file="attachments/customer_complaint/x/c.png",
            original_name="c.png", size=1,
        )
        with self.assertNumQueries(1):  # 仅一次 GROUP BY（content_type 走缓存）
            inject_counts(objs)
        self.assertEqual([o.attachment_count for o in objs], [2, 0, 1])

    def test_panel_context_permission_flag(self):
        user = User.objects.create_user("u2", password="x")
        complaint = CustomerComplaint.objects.create(
            customer_name="测试客户", responsible_salesperson="小张", complaint_date="2026-01-01"
        )
        ctx = panel_context(_FakeRequest(user), complaint)
        self.assertFalse(ctx["can_manage_attachments"])  # 无任何权限
        self.assertEqual(list(ctx["attachments"]), [])
        self.assertTrue(ctx["attachment_categories"])

    def test_media_module_mapping(self):
        from work_record.urls import media_module_for

        self.assertEqual(
            media_module_for("attachments/complaint/complaint/1/a.png"), "complaint"
        )
        self.assertEqual(
            media_module_for("attachments/insurance/insurance/1/a.png"),
            "insurance",
        )
        # 未登记的二级目录（伪造模块名）一律拒认
        self.assertIsNone(media_module_for("attachments/notamodule/x/1/a.png"))
        self.assertIsNone(media_module_for("attachments/unknown/x/1/a.png"))
        # 原有前缀映射不受影响
        self.assertEqual(media_module_for("archives/A01/scan.png"), "archive")
        self.assertIsNone(media_module_for("thumbs/af1.jpg"))


class _FakeRequest:
    """panel_context 只需要 request.user。"""

    def __init__(self, user):
        self.user = user
