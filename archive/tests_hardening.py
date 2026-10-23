"""archive 的改造验收测试（对应技术审计报告 v4 的 #2 / #5 / #10 / #24 / #26）。

四块内容：
- #2：类别树防环三防线（save() 收口、clean() 报错、表单选项排除后代），
      并验证"已存在的环"不会把页面渲染挂死（向后兼容：老数据仍能打开）。
- #10：证照状态/借出标记下推数据库后，语义必须与 Python @property 完全一致，
      且列表页 SQL 条数不随行数增长。
- #24：年审通过但未填新有效期时，不得静默把有效期清空/推进。
- #5 / #26：媒体文件按模块级鉴权，响应带 nosniff。
"""

import os
import shutil
import tempfile
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import Permission, Role

from .forms import CategoryForm, LicenseForm
from .models import Archive, BorrowRecord, Cabinet, Category, License, LicenseBorrowRecord
from .services.license_query import annotate_status, status_aggregate

User = get_user_model()


def grant(module, actions=("read",), user=None):
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
        if hasattr(user, "_rbac_codes_cache"):
            del user._rbac_codes_cache
    cache.clear()
    return role


class CategoryCycleTests(TestCase):
    """#2：类别树任何写入路径都不能成环。"""

    def setUp(self):
        self.root = Category.objects.create(name="客户档案")
        self.child = Category.objects.create(name="客诉记录", parent=self.root)
        self.grand = Category.objects.create(name="2026年", parent=self.child)

    def test_self_parent_is_rejected(self):
        node = Category(name="自环", parent=self.root)
        node.save()
        node.parent = node
        with self.assertRaises(ValidationError):
            node.save()

    def test_two_node_cycle_rejected_on_save(self):
        """审计 #2 的原始复现路径：两次普通编辑即可成环。"""
        with self.assertRaises(ValidationError):
            self.root.parent = self.grand  # 根挂到自己的孙子上
            self.root.save()

    def test_save_with_update_fields_is_guarded(self):
        """只改 parent 字段的局部 save 也要走校验。"""
        self.root.parent = self.grand
        with self.assertRaises(ValidationError):
            self.root.save(update_fields=["parent"])

    def test_deep_chain_still_allowed(self):
        """防环不能误伤合法的深链路（根→子→孙）。"""
        node = Category.objects.create(name="2026Q1", parent=self.grand)
        self.assertEqual(node.full_path.split("/")[-1], "2026Q1")
        self.assertIn("客户档案", node.full_path)

    def test_clean_reports_parent_field(self):
        self.root.parent = self.grand
        with self.assertRaises(ValidationError) as ctx:
            self.root.clean()
        self.assertIn("parent", ctx.exception.message_dict)

    def test_full_path_warns_instead_of_hanging(self):
        """老库里已存在的环：渲染不挂死，返回告警串（向后兼容）。"""
        # 绕过 save() 直接落库，模拟改造前产生的脏数据
        Category.objects.filter(pk=self.root.pk).update(parent_id=self.grand.pk)
        self.root.refresh_from_db()
        text = self.root.full_path
        self.assertIn("疑似循环引用", text)

    def test_legacy_cycle_page_still_renders(self):
        """已有环的老数据，列表页仍能打开（200 + 告警标记）。"""
        Category.objects.filter(pk=self.root.pk).update(parent_id=self.grand.pk)
        root = User.objects.create_superuser("cat_root", "c@e.com", "x12345678")
        self.client.force_login(root)
        resp = self.client.get(reverse("archive:category_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("疑似循环引用", resp.content.decode())


class CategoryFormTests(TestCase):
    """#2 第三道防线：表单选项里不能出现自身与后代。"""

    def setUp(self):
        self.root = Category.objects.create(name="A")
        self.child = Category.objects.create(name="B", parent=self.root)
        self.grand = Category.objects.create(name="C", parent=self.child)
        self.other = Category.objects.create(name="D")

    def test_descendant_ids_walks_whole_subtree(self):
        ids = CategoryForm._descendant_ids(self.root.pk)
        self.assertEqual(ids, {self.child.pk, self.grand.pk})

    def test_form_excludes_self_and_descendants(self):
        form = CategoryForm(instance=self.root)
        options = set(form.fields["parent"].queryset.values_list("pk", flat=True))
        self.assertNotIn(self.root.pk, options)
        self.assertNotIn(self.child.pk, options)
        self.assertNotIn(self.grand.pk, options)
        self.assertIn(self.other.pk, options)

    def test_form_allows_top_level(self):
        form = CategoryForm(data={"name": "E", "parent": "", "sort_order": 0})
        self.assertTrue(form.is_valid(), form.errors)

    def test_form_rejects_cycle_submission(self):
        form = CategoryForm(
            data={"name": "A", "parent": self.grand.pk, "sort_order": 0}, instance=self.root
        )
        self.assertFalse(form.is_valid())


class LicenseQueryTests(TestCase):
    """#10：注解下推后必须与 @property 语义完全一致。"""

    def setUp(self):
        today = timezone.localdate()
        self.licenses = {
            "expired_by_date": License.objects.create(
                name="过期", code="L1", expire_date=today - timedelta(days=1)
            ),
            "expired_by_review": License.objects.create(
                name="年审过期", code="L2", next_review_date=today - timedelta(days=3)
            ),
            "expiring_date": License.objects.create(
                name="临期", code="L3", expire_date=today + timedelta(days=10)
            ),
            "expiring_review": License.objects.create(
                name="年审临期", code="L4", next_review_date=today + timedelta(days=20)
            ),
            "normal": License.objects.create(
                name="正常", code="L5", expire_date=today + timedelta(days=200)
            ),
            "no_date": License.objects.create(name="无有效期", code="L6"),
        }

    def test_annotation_matches_property(self):
        for key, lic in self.licenses.items():
            from_db = License.objects.get(pk=lic.pk)  # 未注解，走 @property 原逻辑
            annotated = annotate_status(License.objects.filter(pk=lic.pk)).get()
            self.assertEqual(
                annotated.status, from_db.status, f"{key} 的注解值与 @property 不一致"
            )

    def test_expected_values(self):
        annotated = {l.code: l.status for l in annotate_status(License.objects.all())}
        self.assertEqual(annotated["L1"], "expired")
        self.assertEqual(annotated["L2"], "expired")
        self.assertEqual(annotated["L3"], "expiring")
        self.assertEqual(annotated["L4"], "expiring")
        self.assertEqual(annotated["L5"], "normal")
        self.assertEqual(annotated["L6"], "normal")

    def test_borrowed_flag_matches_property(self):
        lic = self.licenses["normal"]
        LicenseBorrowRecord.objects.create(
            license=lic, borrower="张三", due_time=timezone.localdate() + timedelta(days=7)
        )
        annotated = annotate_status(License.objects.filter(pk=lic.pk)).get()
        self.assertTrue(annotated.is_borrowed)
        self.assertTrue(License.objects.get(pk=lic.pk).is_borrowed)

        other = annotate_status(License.objects.filter(pk=self.licenses["no_date"].pk)).get()
        self.assertFalse(other.is_borrowed)

    def test_status_aggregate_matches_python_count(self):
        stats = status_aggregate(annotate_status(License.objects.all()))
        all_licenses = list(License.objects.all())
        self.assertEqual(stats["total"], len(all_licenses))
        self.assertEqual(
            stats["expired_count"], sum(1 for l in all_licenses if l.status == "expired")
        )
        self.assertEqual(
            stats["expiring_count"], sum(1 for l in all_licenses if l.status == "expiring")
        )

    def test_license_list_query_count_is_constant(self):
        """SQL 条数不随行数增长（改造前 100 行 = 323 条）。"""
        root = User.objects.create_superuser("lic_root", "l@e.com", "x12345678")
        self.client.force_login(root)
        # 预热一次：首个请求会顺带做一次性的系统查询（session / 权限缓存建立），
        # 不计入稳态比较，否则测试结果会随用例执行顺序漂移。
        self.client.get(reverse("archive:license_list"))

        with CaptureQueriesContext(connection) as small:
            self.client.get(reverse("archive:license_list"))

        for i in range(40):
            lic = License.objects.create(name=f"批量{i}", code=f"B{i}")
            LicenseBorrowRecord.objects.create(
                license=lic,
                borrower="李四",
                due_time=timezone.localdate() + timedelta(days=5),
            )
        with CaptureQueriesContext(connection) as large:
            resp = self.client.get(reverse("archive:license_list"))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            len(small),
            len(large),
            f"SQL 条数随行数增长：{len(small)} -> {len(large)}（疑似回归 N+1）",
        )
        self.assertLess(len(large), 20, f"证照列表 SQL 过多：{len(large)}")


class AnnualReviewCompatTests(TestCase):
    """#24：年审通过但未填新有效期时，不得静默改动原有效期。"""

    def setUp(self):
        self.root = User.objects.create_superuser("rv_root", "r@e.com", "x12345678")
        grant("archive", ("read", "create", "update"), self.root)
        self.lic = License.objects.create(
            name="营业执照",
            code="ZZ-001",
            issue_date=timezone.localdate() - timedelta(days=365),
            expire_date=timezone.localdate() + timedelta(days=30),
            review_cycle=12,
        )

    def test_passed_without_new_expire_keeps_dates(self):
        old_expire = self.lic.expire_date
        old_review = self.lic.next_review_date
        self.client.force_login(self.root)
        resp = self.client.post(
            reverse("archive:annual_review_create", args=[self.lic.pk]),
            {
                "review_date": timezone.localdate().isoformat(),
                "result": "passed",
                "reviewer": "王五",
                "remark": "",
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.lic.refresh_from_db()
        self.assertEqual(self.lic.expire_date, old_expire, "未填新有效期时不得清空/改动有效期")
        self.assertEqual(self.lic.next_review_date, old_review)
        self.assertContains(resp, "本次未填写新有效期")

    def test_passed_with_new_expire_advances_dates(self):
        new_expire = timezone.localdate() + timedelta(days=1500)
        self.client.force_login(self.root)
        self.client.post(
            reverse("archive:annual_review_create", args=[self.lic.pk]),
            {
                "review_date": timezone.localdate().isoformat(),
                "result": "passed",
                "reviewer": "王五",
                "new_expire_date": new_expire.isoformat(),
                "remark": "",
            },
            follow=True,
        )
        self.lic.refresh_from_db()
        self.assertEqual(self.lic.expire_date, new_expire)
        self.assertIsNotNone(self.lic.next_review_date)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="arc_media_"))
class ProtectedMediaTests(TestCase):
    """#5 / #26：媒体文件按模块鉴权，禁止登录即可下载。"""

    @classmethod
    def tearDownClass(cls):
        from django.conf import settings

        shutil.rmtree(settings.MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        from django.conf import settings

        target_dir = os.path.join(settings.MEDIA_ROOT, "archives")
        os.makedirs(target_dir, exist_ok=True)
        self.rel_path = "archives/scan.txt"
        with open(os.path.join(settings.MEDIA_ROOT, self.rel_path), "w", encoding="utf-8") as fh:
            fh.write("secret")

    def test_anonymous_is_redirected_to_login(self):
        resp = self.client.get(f"/media/{self.rel_path}")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])

    def test_user_without_archive_perm_is_forbidden(self):
        user = User.objects.create_user("m_none", "m@e.com", "x12345678")
        self.client.force_login(user)
        resp = self.client.get(f"/media/{self.rel_path}")
        self.assertEqual(resp.status_code, 403)

    def test_user_with_archive_perm_gets_file(self):
        user = User.objects.create_user("m_arch", "m2@e.com", "x12345678")
        grant("archive", ("read",), user)
        self.client.force_login(user)
        resp = self.client.get(f"/media/{self.rel_path}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["X-Content-Type-Options"], "nosniff")
        self.assertEqual(b"".join(resp.streaming_content), b"secret")

    def test_unregistered_prefix_is_404(self):
        user = User.objects.create_superuser("m_root", "m3@e.com", "x12345678")
        self.client.force_login(user)
        self.assertEqual(self.client.get("/media/unknown/a.txt").status_code, 404)

    def test_missing_file_is_404_after_authz(self):
        user = User.objects.create_superuser("m_root2", "m4@e.com", "x12345678")
        self.client.force_login(user)
        self.assertEqual(self.client.get("/media/archives/nope.txt").status_code, 404)

    def test_html_file_is_forced_attachment(self):
        """审计 P1-2：用户上传的 .html 不得在同源下被浏览器执行。"""
        from django.conf import settings

        path = os.path.join(settings.MEDIA_ROOT, "archives", "evil.html")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("<script>alert(1)</script>")
        user = User.objects.create_superuser("m_root3", "m5@e.com", "x12345678")
        self.client.force_login(user)
        resp = self.client.get("/media/archives/evil.html")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Disposition"].startswith("attachment"))
        self.assertEqual(resp["X-Content-Type-Options"], "nosniff")

    def test_pdf_is_inline_with_sameorigin_for_iframe(self):
        """审计 P2-3：PDF 需允许同源 iframe 预览，同时用 CSP 兜底。"""
        from django.conf import settings

        path = os.path.join(settings.MEDIA_ROOT, "archives", "preview.pdf")
        with open(path, "wb") as fh:
            fh.write(b"%PDF-1.4\n%%EOF")
        user = User.objects.create_superuser("m_root4", "m6@e.com", "x12345678")
        self.client.force_login(user)
        resp = self.client.get("/media/archives/preview.pdf")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Disposition"].startswith("inline"))
        self.assertEqual(resp["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn("frame-ancestors 'self'", resp["Content-Security-Policy"])


class UploadValidationTests(TestCase):
    """审计 SEC-02 / SEC-08：导入文件与档案附件必须有类型、大小、文件头三重校验。"""

    def test_import_rejects_oversized_file(self):
        from unittest import mock

        from .validators import validate_import_file

        f = SimpleUploadedFile("a.csv", b"a,b\n1,2\n")
        with mock.patch("archive.validators.IMPORT_MAX_SIZE", 4):
            with self.assertRaisesMessage(ValueError, "超过上限"):
                validate_import_file(f)

    def test_import_rejects_exe_renamed_xlsx(self):
        from .services.import_export import _parse_excel_sheets

        f = SimpleUploadedFile("evil.xlsx", b"MZ\x90\x00" + b"\x00" * 64)
        with self.assertRaisesMessage(ValueError, "文件内容与扩展名不符"):
            _parse_excel_sheets(f)

    def test_import_accepts_valid_csv(self):
        from .services.import_export import _read_import_rows

        f = SimpleUploadedFile("ok.csv", "编号,名称\nARC1,测试档案\n".encode("utf-8-sig"))
        rows = _read_import_rows(f)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["名称"], "测试档案")

    def test_archive_attachment_rejects_pe_renamed_pdf(self):
        from .validators import validate_archive_file

        err = validate_archive_file(SimpleUploadedFile("fake.pdf", b"MZ\x90\x00" + b"\x00" * 64))
        self.assertIn("内容与扩展名不符", err)

    def test_archive_attachment_accepts_real_pdf(self):
        from .validators import validate_archive_file

        err = validate_archive_file(SimpleUploadedFile("ok.pdf", b"%PDF-1.4\n%%EOF"))
        self.assertEqual(err, "")


class LicenseScanValidationTests(TestCase):
    """审计 P1-2：证照扫描件必须校验扩展名、文件头与大小。"""

    def _form(self, name, content):
        return LicenseForm(
            data={"code": "LIC-T1", "name": "营业执照", "review_cycle": "0"},
            files={"scan_file": SimpleUploadedFile(name, content)},
        )

    def test_html_extension_is_rejected(self):
        form = self._form("evil.html", b"<script>alert(1)</script>")
        self.assertFalse(form.is_valid())
        self.assertIn("scan_file", form.errors)

    def test_svg_extension_is_rejected(self):
        form = self._form("evil.svg", b"<svg onload=alert(1)></svg>")
        self.assertFalse(form.is_valid())

    def test_extension_magic_mismatch_is_rejected(self):
        form = self._form("fake.jpg", b"<html>not an image</html>")
        self.assertFalse(form.is_valid())
        self.assertIn("内容与扩展名不符", str(form.errors["scan_file"]))

    def test_valid_pdf_passes(self):
        form = self._form("scan.pdf", b"%PDF-1.4\n%%EOF")
        self.assertTrue(form.is_valid(), form.errors)

    def test_valid_png_passes(self):
        form = self._form("scan.png", b"\x89PNG\r\n\x1a\n" + b"0" * 16)
        self.assertTrue(form.is_valid(), form.errors)


class ArchiveLocationUniqueTests(TestCase):
    """R1：柜/层/格三非空时由数据库兜底唯一，堵住"先查后写"的并发/双击脏数据。

    表单校验（ArchiveForm.clean）与批量导入都是"先查后写"，并发下会静默产生
    "一格两档"。数据层约束是唯一可靠的最终防线。
    """

    def setUp(self):
        self.cab = Cabinet.objects.create(code="C1")

    def test_duplicate_full_location_raises_integrity_error(self):
        Archive(code="A1", name="档案1", cabinet=self.cab, layer=1, cell=1).save()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Archive(code="A2", name="档案2", cabinet=self.cab, layer=1, cell=1).save()

    def test_null_location_is_not_constrained(self):
        """柜/层/格全空时不受约束，允许多条（历史数据大量为空）。"""
        Archive(code="A1", name="档案1").save()
        Archive(code="A2", name="档案2").save()
        self.assertEqual(Archive.objects.filter(cabinet__isnull=True).count(), 2)

    def test_partial_location_is_not_constrained(self):
        """只填了一部分（缺层/格）时不参与唯一约束，避免误伤半填数据。"""
        Archive(code="A1", name="档案1", cabinet=self.cab).save()
        Archive(code="A2", name="档案2", cabinet=self.cab).save()
        Archive(code="A3", name="档案3", cabinet=self.cab, layer=1).save()
        Archive(code="A4", name="档案4", cabinet=self.cab, layer=1).save()
        self.assertEqual(Archive.objects.count(), 4)

    def test_move_to_occupied_cell_raises(self):
        """移动档案到已占用格号时被数据库拦下（archive_move 的写入路径）。"""
        a1 = Archive(code="A1", name="档案1", cabinet=self.cab, layer=1, cell=1)
        a1.save()
        a2 = Archive(code="A2", name="档案2", cabinet=self.cab, layer=2, cell=2)
        a2.save()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                a2.layer, a2.cell = 1, 1
                a2.save(update_fields=["layer", "cell"])

    def test_view_create_duplicate_location_reports_error(self):
        """archive_create 捕获位置冲突后回填表单错误（422），不产生第二条记录。"""
        root = User.objects.create_superuser("alu_root", "alu@e.com", "x12345678")
        grant("archive", ("read", "create"), root)
        Archive(code="A1", name="档案1", cabinet=self.cab, layer=1, cell=1).save()
        self.client.force_login(root)
        resp = self.client.post(
            reverse("archive:archive_create"),
            {"code": "A2", "name": "档案2", "cabinet": self.cab.pk, "layer": "1", "cell": "1"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 422)
        self.assertIn("已被占用", resp.json()["html"])
        self.assertEqual(
            Archive.objects.filter(cabinet=self.cab, layer=1, cell=1).count(), 1
        )


class BorrowUniqueTests(TestCase):
    """R2：同一档案同一时刻只允许一条未归还记录。"""

    def setUp(self):
        self.arc = Archive.objects.create(code="B1", name="借阅档案")
        self.user = User.objects.create_user("bo_user", "bo@e.com", "x12345678")
        grant("archive", ("read", "create", "update"), self.user)

    def _borrow(self, borrower="张三"):
        return BorrowRecord.objects.create(
            archive=self.arc,
            borrower=borrower,
            due_time=timezone.localdate() + timedelta(days=7),
        )

    def test_second_open_borrow_raises(self):
        self._borrow()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._borrow("李四")

    def test_returned_record_allows_new_borrow(self):
        """已归还后可以再次借出——约束只挡"未归还"。"""
        first = self._borrow()
        first.return_time = timezone.now()
        first.refresh_status()
        first.save(update_fields=["return_time", "status"])
        self._borrow("李四")
        self.assertEqual(BorrowRecord.objects.filter(archive=self.arc).count(), 2)

    def test_view_duplicate_borrow_returns_422(self):
        self.client.force_login(self.user)
        self._borrow()
        resp = self.client.post(
            reverse("archive:borrow_create", args=[self.arc.pk]),
            {
                "borrower": "李四",
                "due_time": (timezone.localdate() + timedelta(days=7)).isoformat(),
                "remark": "",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 422)
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(
            BorrowRecord.objects.filter(archive=self.arc, return_time__isnull=True).count(), 1
        )


class LicenseBorrowUniqueTests(TestCase):
    """R2：证照借阅与档案借阅同构，同一证照同时只允许一条未归还记录。"""

    def setUp(self):
        self.lic = License.objects.create(name="营业执照", code="LX-1")
        self.user = User.objects.create_user("lb_user", "lb@e.com", "x12345678")
        grant("archive", ("read", "create", "update"), self.user)

    def _borrow(self, borrower="张三"):
        return LicenseBorrowRecord.objects.create(
            license=self.lic,
            borrower=borrower,
            due_time=timezone.localdate() + timedelta(days=7),
        )

    def test_second_open_borrow_raises(self):
        self._borrow()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._borrow("李四")

    def test_returned_record_allows_new_borrow(self):
        first = self._borrow()
        first.return_time = timezone.now()
        first.refresh_status()
        first.save(update_fields=["return_time", "status"])
        self._borrow("李四")
        self.assertEqual(LicenseBorrowRecord.objects.filter(license=self.lic).count(), 2)

    def test_view_duplicate_license_borrow_returns_422(self):
        self.client.force_login(self.user)
        self._borrow()
        resp = self.client.post(
            reverse("archive:license_borrow_create", args=[self.lic.pk]),
            {
                "borrower": "李四",
                "due_time": (timezone.localdate() + timedelta(days=7)).isoformat(),
                "remark": "",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 422)
        self.assertFalse(resp.json()["ok"])
