"""批次B 验收测试：知识库阅读记录表（KnowledgeView）+ 阅读报告页。

覆盖：
1. 详情页打开 → 写一行 KnowledgeView（阅读人 / 来源 IP 落库），view_count 自增；
   详情弹窗已取消：带 AJAX 请求头也按整页渲染并计入，不再有预览特例。
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
            visibility="public",  # 存量文档口径：迁移 0003 已将无 owner 存量置公开
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

    def test_ajax_header_renders_full_page(self):
        """详情弹窗已取消：带 X-Requested-With 头也按整页渲染并计入浏览。"""
        c = Client()
        c.force_login(self.user)
        resp = c.get(
            reverse("knowledge_base:document_detail", args=[self.doc.pk]),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp["Content-Type"])
        self.assertIn("测试文档", resp.content.decode())
        # 弹窗取消后不再有「预览不计入」的特例
        self.assertTrue(KnowledgeView.objects.exists())
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.view_count, 1)


class KnowledgeReportTests(TestCase):
    def setUp(self):
        self.category = KnowledgeCategory.objects.create(name="报告分类")
        self.doc1 = KnowledgeDocument.objects.create(
            title="文档一", category=self.category, content="a", status="published",
            visibility="public",
        )
        self.doc2 = KnowledgeDocument.objects.create(
            title="文档二", category=self.category, content="b", status="published",
            visibility="public",
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


# ---------------------------------------------------------------------------
# 批次 1 验收：可见性内核（交叉账号矩阵）
# 矩阵口径：新文档默认私有；公开必须显式；收回立即生效；泄漏口全封。
# ---------------------------------------------------------------------------

from django.core.cache import cache as _cache  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from records.models import OperationLog  # noqa: E402

from . import access as kb_access  # noqa: E402


class VisibilityMatrixTests(TestCase):
    """A/B 双账号矩阵：私有文档在全部读路径上都不可见。"""

    def setUp(self):
        self.category = KnowledgeCategory.objects.create(name="可见性测试分类")
        self.alice = User.objects.create_user("kb_alice", "alice@e.com", PWD, is_active=True)
        self.bob = User.objects.create_user("kb_bob", "bob@e.com", PWD, is_active=True)
        self.root = User.objects.create_superuser("kb_root", "root@e.com", PWD)
        grant(self.alice, ("knowledge", "read"), ("knowledge", "create"),
              ("knowledge", "update"), ("knowledge", "delete"))
        grant(self.bob, ("knowledge", "read"), ("knowledge", "create"),
              ("knowledge", "update"), ("knowledge", "delete"))
        self.private_doc = KnowledgeDocument.objects.create(
            title="Alice私人笔记", category=self.category, content="客户报价 99 万",
            status="published", owner=self.alice, visibility="private",
        )
        self.public_doc = KnowledgeDocument.objects.create(
            title="Alice公开手册", category=self.category, content="公开内容",
            status="published", owner=self.alice, visibility="public",
        )
        self.orphan_private = KnowledgeDocument.objects.create(
            title="孤儿私有文档", category=self.category, content="owner 注销了",
            status="published", owner=None, visibility="private",
        )

    def tearDown(self):
        _cache.clear()

    def _login(self, user):
        c = Client()
        c.force_login(user)
        return c

    # ---- access.py 单元行为 ----

    def test_default_visibility_is_private(self):
        """新建文档不带 visibility 时默认私有（write path 兜底）。"""
        doc = KnowledgeDocument.objects.create(title="默认私有", status="published")
        self.assertEqual(doc.visibility, "private")

    def test_admin_cannot_read_private_by_default(self):
        """D1 拍板：连超管也看不到别人的私有文档，「仅自己可见」是句真话。"""
        self.assertFalse(kb_access.can_view(self.private_doc, self.root))
        self.assertFalse(kb_access.can_view(self.private_doc, self.bob))

    def test_orphan_private_not_public(self):
        """owner 注销（SET_NULL）后的私有文档不能自动变成全员可见。"""
        self.assertFalse(kb_access.can_view(self.orphan_private, self.bob))
        self.assertFalse(kb_access.can_view(self.orphan_private, self.alice))

    # ---- 页面读路径：列表 / 详情 / 搜索 / 编辑 / 删除 ----

    def test_bob_list_hides_private_docs(self):
        resp = self._login(self.bob).get(reverse("knowledge_base:document_list"))
        titles = {d.title for d in resp.context["documents"]}
        self.assertIn("Alice公开手册", titles)
        self.assertNotIn("Alice私人笔记", titles)
        self.assertNotIn("孤儿私有文档", titles)

    def test_category_count_matches_visible_list(self):
        """分类计数必须与列表条数一致，否则「42 篇」点进去只有 3 条。"""
        resp = self._login(self.bob).get(reverse("knowledge_base:document_list"))
        counts = {c.name: c.doc_count for c in resp.context["categories"]}
        self.assertEqual(counts["可见性测试分类"], 1)

    def test_bob_detail_private_is_404(self):
        resp = self._login(self.bob).get(
            reverse("knowledge_base:document_detail", args=[self.private_doc.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_bob_search_cannot_hit_private_content(self):
        resp = self._login(self.bob).get(
            reverse("knowledge_base:document_search"), {"keyword": "99 万"})
        self.assertEqual(list(resp.context["documents"]), [])

    def test_bob_cannot_edit_public_doc(self):
        """公开文档他人只读：「他公开了」不等于「大家都能改」。"""
        resp = self._login(self.bob).get(
            reverse("knowledge_base:document_edit", args=[self.public_doc.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_bob_edit_private_is_404_not_403(self):
        """403 等于告诉对方「这篇存在，只是你没权限」——必须 404。"""
        resp = self._login(self.bob).get(
            reverse("knowledge_base:document_edit", args=[self.private_doc.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_bob_cannot_delete_public_doc(self):
        resp = self._login(self.bob).post(
            reverse("knowledge_base:document_delete", args=[self.public_doc.pk]))
        self.assertIn(resp.status_code, (403, 405))
        self.assertTrue(KnowledgeDocument.objects.filter(pk=self.public_doc.pk).exists())

    def test_alice_can_edit_own_private_doc(self):
        resp = self._login(self.alice).get(
            reverse("knowledge_base:document_edit", args=[self.private_doc.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_create_assigns_owner_and_private_default(self):
        resp = self._login(self.bob).post(
            reverse("knowledge_base:document_create"),
            {"title": "Bob的新文档", "content": "x", "status": "published",
             "category": self.category.pk},
        )
        self.assertIn(resp.status_code, (200, 302))
        doc = KnowledgeDocument.objects.get(title="Bob的新文档")
        self.assertEqual(doc.owner, self.bob)
        self.assertEqual(doc.visibility, "private")

    # ---- 站外读路径：全局搜索 / 统计 ----

    def test_global_search_hides_private_docs(self):
        resp = self._login(self.bob).get(reverse("search"), {"keyword": "Alice"})
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8", errors="ignore")
        self.assertNotIn("Alice私人笔记", body)

    def test_report_only_counts_visible_docs(self):
        """阅读报告榜单不把他人私有文档标题摆出来（保守口径）。"""
        KnowledgeView.objects.create(
            document=self.private_doc, user=self.alice, viewer="kb_alice", ip="10.0.0.9")
        resp = self._login(self.root).get(reverse("knowledge_report"))
        titles = [row["document__title"] for row in resp.context["top_documents"]]
        self.assertNotIn("Alice私人笔记", titles)

    # ---- DRF / AI 读路径 ----

    def test_viewset_hides_private_docs(self):
        client = APIClient()
        client.force_login(self.bob)
        resp = client.get("/api/knowledge/document/")
        self.assertEqual(resp.status_code, 200)
        titles = {row["title"] for row in resp.data["results"]}
        self.assertNotIn("Alice私人笔记", titles)
        self.assertIn("Alice公开手册", titles)

    def test_viewset_detail_private_is_404(self):
        client = APIClient()
        client.force_login(self.bob)
        resp = client.get(f"/api/knowledge/document/{self.private_doc.pk}/")
        self.assertEqual(resp.status_code, 404)

    def test_ai_summary_count_uses_visible_scope(self):
        client = APIClient()
        client.force_login(self.bob)
        resp = client.get("/api/ai/summary/")
        self.assertEqual(resp.status_code, 200)
        # bob 可见：公开手册 1 篇（两篇私有一篇是 Alice 的、一篇是孤儿）
        self.assertEqual(resp.data["总数据统计"]["知识文档"], 1)

    def test_model_list_count_uses_visible_scope(self):
        client = APIClient()
        client.force_login(self.bob)
        resp = client.get("/api/ai/models/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["knowledge_document"]["count"], 1)

    # ---- 可见性切换 + 留痕 ----

    def test_set_visibility_public_then_revoke(self):
        """公开 → 收回立即生效；两次切换都要留痕。"""
        request_ok = kb_access.set_visibility(
            self.private_doc, "public", actor=self.alice, source="单元测试")
        self.assertTrue(request_ok)
        self.private_doc.refresh_from_db()
        self.assertEqual(self.private_doc.visibility, "public")
        self.assertTrue(kb_access.can_view(self.private_doc, self.bob))

        kb_access.set_visibility(self.private_doc, "private", actor=self.alice, source="单元测试")
        self.private_doc.refresh_from_db()
        self.assertEqual(self.private_doc.visibility, "private")
        self.assertFalse(kb_access.can_view(self.private_doc, self.bob))

        logs = OperationLog.objects.filter(
            module_type="知识库", object_identifier=f"知识文档#{self.private_doc.pk}"
        ).order_by("id")
        self.assertEqual(logs.count(), 2)
        self.assertIn("仅自己可见 → 全员可见", logs.first().change_summary)
        self.assertIn("全员可见 → 仅自己可见", logs.last().change_summary)

    def test_set_visibility_noop_returns_false(self):
        self.assertFalse(kb_access.set_visibility(self.private_doc, "private", actor=self.alice))
        self.assertEqual(
            OperationLog.objects.filter(
                module_type="知识库", object_identifier=f"知识文档#{self.private_doc.pk}").count(), 0)

    def test_invalid_visibility_value_rejected(self):
        with self.assertRaises(ValueError):
            kb_access.set_visibility(self.private_doc, "everyone", actor=self.alice)

    # ---- 批次 2：可见性 UI ----

    def test_visibility_toggle_endpoint_roundtrip(self):
        """详情页切换接口：GET 拒绝、owner POST 生效、非法值 400、他人 403。"""
        url = reverse("knowledge_base:document_visibility", args=[self.private_doc.pk])
        c = self._login(self.alice)
        self.assertEqual(c.get(url).status_code, 405)

        resp = c.post(url, {"visibility": "public"})
        self.assertEqual(resp.status_code, 200)
        self.private_doc.refresh_from_db()
        self.assertEqual(self.private_doc.visibility, "public")

        self.assertEqual(c.post(url, {"visibility": "everyone"}).status_code, 400)

        resp = self._login(self.bob).post(
            reverse("knowledge_base:document_visibility", args=[self.public_doc.pk]),
            {"visibility": "private"})
        self.assertEqual(resp.status_code, 403)
        self.public_doc.refresh_from_db()
        self.assertEqual(self.public_doc.visibility, "public")

    def test_create_form_renders_visibility_radios(self):
        resp = self._login(self.bob).get(reverse("knowledge_base:document_create"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('name="visibility"', body)
        self.assertIn("仅自己可见", body)

    def test_detail_toggle_button_owner_only(self):
        """切换按钮只有创建人能看到；他人连按钮都不渲染（而不是点了才 403）。"""
        detail_private = reverse("knowledge_base:document_detail", args=[self.private_doc.pk])
        body_alice = self._login(self.alice).get(detail_private).content.decode()
        self.assertIn('id="kbVisToggle"', body_alice)

        detail_public = reverse("knowledge_base:document_detail", args=[self.public_doc.pk])
        body_bob = self._login(self.bob).get(detail_public).content.decode()
        self.assertNotIn('id="kbVisToggle"', body_bob)
        # bob 能看到公开文档上的可见性徽章
        self.assertIn("全员可见", body_bob)

    def test_detail_page_ui_ignores_ajax_header(self):
        """详情弹窗已取消：带 X-Requested-With 头也渲染同一张整页。

        此前列表页走 AJAX 弹窗、其余入口走整页，两条路径 UI 曾不一致；
        现在合并为整页，可见性 UI 完整（徽章两态 + 切换按钮仅创建人）。
        """
        detail_private = reverse("knowledge_base:document_detail", args=[self.private_doc.pk])
        body_alice = self._login(self.alice).get(
            detail_private, HTTP_X_REQUESTED_WITH="XMLHttpRequest").content.decode()
        self.assertIn('id="kbVisBadge"', body_alice)
        self.assertIn('id="kbVisToggle"', body_alice)
        self.assertIn("仅自己可见", body_alice)
        self.assertIn("设为全员可见", body_alice)

        # 公开文档：徽章同样显示（此前的弹窗模板公开时连徽章都不渲染）
        detail_public = reverse("knowledge_base:document_detail", args=[self.public_doc.pk])
        body_bob = self._login(self.bob).get(
            detail_public, HTTP_X_REQUESTED_WITH="XMLHttpRequest").content.decode()
        self.assertIn('id="kbVisBadge"', body_bob)
        self.assertIn("全员可见", body_bob)
        self.assertNotIn('id="kbVisToggle"', body_bob)


class MarkdownSanitizeTests(TestCase):
    """审计 SEC-06：知识库是唯一 |safe 出口，白名单必须防漂移。"""

    def test_sanitize_rejects_script_tag(self):
        from .views import _sanitize_html

        out = _sanitize_html('<p>ok</p><script>alert(1)</script>')
        self.assertNotIn("<script", out.lower())
        self.assertIn("ok", out)

    def test_sanitize_rejects_onerror_attr(self):
        from .views import _sanitize_html

        out = _sanitize_html('<img src="x" onerror="alert(1)">')
        self.assertNotIn("onerror", out.lower())

    def test_sanitize_rejects_javascript_protocol(self):
        from .views import _sanitize_html

        out = _sanitize_html('<a href="javascript:alert(1)">点我</a>')
        self.assertNotIn("javascript:", out.lower())

    def test_render_markdown_keeps_normal_link_and_strips_script(self):
        from .views import _render_markdown

        out = _render_markdown("[官网](https://example.com)\n\n<script>x</script>")
        self.assertIn('href="https://example.com"', out)
        self.assertNotIn("<script", out.lower())
