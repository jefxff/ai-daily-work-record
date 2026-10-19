"""批次B 验收测试：知识库阅读记录表（KnowledgeView）+ 阅读报告页。

覆盖：
1. 详情页非 AJAX 打开 → 写一行 KnowledgeView（阅读人 / 来源 IP 落库），view_count 自增；
   AJAX 弹窗预览 → 不写阅读记录（同一次浏览不重复计数）。
2. 阅读报告页权限：仅 system.read 可访问（仅有 knowledge.read → 403，匿名 → 302）。
3. 阅读报告聚合正确：窗口内阅读总量 / 独立阅读人 / 覆盖文档数 / 热门文档榜。

运行：python manage.py test knowledge_base
"""
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from accounts.models import Permission, Role
from accounts.registry import permission_code

from .models import KnowledgeCategory, KnowledgeDocument, KnowledgeView

PWD = "X12345678"


def grant(user, *pairs):
    """按 (module, action) 授权（Permission 行缺失时补建，避免依赖 RBAC 种子迁移）。"""
    role = Role.objects.create(code=f"kb-test-{user.username}", name="知识库测试角色")
    for module, action in pairs:
        perm, _ = Permission.objects.get_or_create(
            module=module, action=action,
            defaults={"code": permission_code(module, action), "name": f"{module}-{action}"},
        )
        role.permissions.add(perm)
    role.users.add(user)
    # _codes() 会缓存到 user 实例上，授权后必须清缓存
    if hasattr(user, "_rbac_codes_cache"):
        del user._rbac_codes_cache
    cache.clear()


class KnowledgeViewRecordTests(TestCase):
    def setUp(self):
        self.category = KnowledgeCategory.objects.create(name="测试分类")
        self.doc = KnowledgeDocument.objects.create(
            title="测试文档", category=self.category,
            content="# 标题\n正文内容", status="published",
        )
        self.user = User.objects.create_user("kbreader", "k@e.com", PWD, is_active=True)
        grant(self.user, ("knowledge", "read"))

    def tearDown(self):
        cache.clear()

    def test_detail_page_writes_view_record(self):
        c = Client()
        c.force_login(self.user)
        resp = c.get(reverse("knowledge_base:document_detail", args=[self.doc.pk]))
        self.assertEqual(resp.status_code, 200)

        record = KnowledgeView.objects.get()
        self.assertEqual(record.document_id, self.doc.pk)
        self.assertEqual(record.user, self.user)
        self.assertEqual(record.viewer, "kbreader")
        self.assertNotEqual(record.ip, "")

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.view_count, 1)

    def test_repeat_visits_keep_each_row(self):
        """每次浏览一行、全部保留（不去重）。"""
        c = Client()
        c.force_login(self.user)
        url = reverse("knowledge_base:document_detail", args=[self.doc.pk])
        c.get(url)
        c.get(url)
        self.assertEqual(KnowledgeView.objects.count(), 2)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.view_count, 2)

    def test_ajax_preview_does_not_write(self):
        c = Client()
        c.force_login(self.user)
        resp = c.get(
            reverse("knowledge_base:document_detail", args=[self.doc.pk]),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(KnowledgeView.objects.exists())
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.view_count, 0)


class KnowledgeReportTests(TestCase):
    def setUp(self):
        self.category = KnowledgeCategory.objects.create(name="报告分类")
        self.doc1 = KnowledgeDocument.objects.create(
            title="文档一", category=self.category, content="a", status="published",
        )
        self.doc2 = KnowledgeDocument.objects.create(
            title="文档二", category=self.category, content="b", status="published",
        )
        self.reader_a = User.objects.create_user("reader_a", "a@e.com", PWD, is_active=True)
        self.reader_b = User.objects.create_user("reader_b", "b@e.com", PWD, is_active=True)
        self.kb_only = User.objects.create_user("kb_only", "k@e.com", PWD, is_active=True)
        self.sys_reader = User.objects.create_user("sys_reader", "s@e.com", PWD, is_active=True)

        grant(self.kb_only, ("knowledge", "read"))
        grant(self.sys_reader, ("system", "read"))

        KnowledgeView.objects.create(document=self.doc1, user=self.reader_a, viewer="reader_a", ip="10.0.0.1")
        KnowledgeView.objects.create(document=self.doc1, user=self.reader_a, viewer="reader_a", ip="10.0.0.1")
        KnowledgeView.objects.create(document=self.doc2, user=self.reader_b, viewer="reader_b", ip="10.0.0.2")

    def tearDown(self):
        cache.clear()

    def _login(self, user):
        c = Client()
        c.force_login(user)
        return c

    def test_permission_requires_system_read(self):
        resp = self._login(self.kb_only).get(reverse("knowledge_report"))
        self.assertEqual(resp.status_code, 403)

        resp = self._login(self.sys_reader).get(reverse("knowledge_report"))
        self.assertEqual(resp.status_code, 200)

    def test_anonymous_redirects_to_login(self):
        resp = Client().get(reverse("knowledge_report"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp.url)

    def test_aggregates_within_window(self):
        resp = self._login(self.sys_reader).get(reverse("knowledge_report"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_views"], 3)
        self.assertEqual(resp.context["unique_viewers"], 2)
        self.assertEqual(resp.context["viewed_docs"], 2)
        self.assertEqual(resp.context["published_docs"], 2)
        self.assertEqual(resp.context["unread_docs"], 0)

        top = resp.context["top_documents"]
        self.assertEqual(top[0]["document_id"], self.doc1.pk)
        self.assertEqual(top[0]["cnt"], 2)

    def test_days_param_clamped(self):
        c = self._login(self.sys_reader)
        url = reverse("knowledge_report")

        self.assertEqual(c.get(url, {"days": "abc"}).context["days"], 30)
        self.assertEqual(c.get(url, {"days": "0"}).context["days"], 1)
        self.assertEqual(c.get(url, {"days": "9999"}).context["days"], 365)
