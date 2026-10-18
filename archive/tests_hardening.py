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
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import Permission, Role

from .forms import CategoryForm
from .models import Category, License, LicenseBorrowRecord
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
