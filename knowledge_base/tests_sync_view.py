"""每日记录沉淀容器的阅读视图回归（UI 反馈 #4）。

容器文档正文只有占位说明，真实内容在节点树；此前阅读视图不渲染节点，
点开文档看不到任何每日记录内容。修复后节点正文按排序内联进阅读流。
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from knowledge_base.models import KnowledgeCategory, KnowledgeDocument, KnowledgeNode

User = get_user_model()


class SyncContainerReadingViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("sink_user", "sink@e.com", "x12345678")
        cls.cat = KnowledgeCategory.objects.create(name="工作沉淀")

    def _make_container(self):
        return KnowledgeDocument.objects.create(
            title="2026年10月工作沉淀",
            category=self.cat,
            owner=self.user,
            status="published",
            visibility="private",
            source="daily_record_sync",
            content="由每日记录自动沉淀生成，按月归档。记录为本体，此处为同步视图。",
            tags="daily_record_sync,2026-10",
        )

    def test_reading_view_renders_node_content(self):
        doc = self._make_container()
        from records.models import DailyRecord
        rec = DailyRecord.objects.create(
            user=self.user, work_content="今天完成了登录模块的自测。",
            status="完成")
        # record_date 是 auto_now_add，create 时传值会被忽略；用 update 固定到
        # 断言里的日期，否则这条用例只在系统日期恰为 2026-10-25 时通过。
        DailyRecord.objects.filter(pk=rec.pk).update(
            record_date="2026-10-25T09:00:00+08:00")
        KnowledgeNode.objects.create(
            document=doc, uid="r0000000001", parent=None, sort=0,
            text="今天完成了登录模块的自测。", node_type="todo",
            is_done=True, ref_module="daily_record", ref_object_id=rec.pk,
            ref_label="[完成|100%|截至2026-10-25] 今天完成了登录模块的自测。",
        )
        self.client.force_login(self.user)
        resp = self.client.get(reverse("knowledge_base:document_detail", args=[doc.pk]))
        self.assertEqual(resp.status_code, 200)
        # 按天分组渲染：日期大标题 + 记录正文
        self.assertContains(resp, '<h2 class="kb-sync-day-title">2026-10-25</h2>')
        self.assertContains(resp, "今天完成了登录模块的自测。")
        # 不渲染容器占位说明，也不渲染状态/进度等元数据
        self.assertNotContains(resp, "记录为本体，此处为同步视图")
        self.assertNotContains(resp, "[完成|100%")

    def test_normal_document_unaffected(self):
        doc = KnowledgeDocument.objects.create(
            title="普通笔记", category=self.cat, owner=self.user,
            status="published", visibility="private", content="普通内容",
        )
        self.client.force_login(self.user)
        resp = self.client.get(reverse("knowledge_base:document_detail", args=[doc.pk]))
        self.assertEqual(resp.status_code, 200)
        # 模板头部常驻沉淀相关 CSS 定义，此处断言不应渲染出天分组 section
        self.assertNotContains(resp, '<section class="kb-sync-day"')


class SyncContainerTreeAndEditTests(TestCase):
    """大纲/导图数据源与编辑弹窗对沉淀容器的表现（UI 反馈 #4 追加）。"""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("sink_user2", "sink2@e.com", "x12345678")
        cls.cat = KnowledgeCategory.objects.create(name="工作沉淀")

    def _make_container_with_record(self):
        from records.models import DailyRecord
        doc = KnowledgeDocument.objects.create(
            title="2026年10月工作沉淀", category=self.cat, owner=self.user,
            status="published", visibility="private", source="daily_record_sync",
            content="占位", tags="工作沉淀,2026-10",
        )
        rec = DailyRecord.objects.create(
            user=self.user, work_content="完成了支付回调的自测。", status="完成")
        # record_date 是 auto_now_add，create 时传值会被忽略，用 update 绕过
        DailyRecord.objects.filter(pk=rec.pk).update(record_date="2026-10-24T15:00:00+08:00")
        KnowledgeNode.objects.create(
            document=doc, uid="r0000000002", parent=None, sort=0,
            text="完成了支付回调的自测。", node_type="todo", is_done=True,
            ref_module="daily_record", ref_object_id=rec.pk, ref_label="x")
        return doc

    def test_tree_api_groups_by_day(self):
        doc = self._make_container_with_record()
        self.client.force_login(self.user)
        resp = self.client.get(reverse("knowledge_base:api_doc_tree", args=[doc.pk]))
        self.assertEqual(resp.status_code, 200)
        tree = resp.json()["tree"]
        self.assertEqual(len(tree), 1)
        day = tree[0]
        self.assertEqual(day["text"], "2026-10-24")
        self.assertEqual(day["type"], "text")
        self.assertEqual([c["text"] for c in day["children"]],
                         ["完成了支付回调的自测。"])
        self.assertTrue(day["children"][0]["done"])

    def test_tree_put_rejected_for_sync_container(self):
        doc = self._make_container_with_record()
        self.client.force_login(self.user)
        resp = self.client.put(
            reverse("knowledge_base:api_doc_tree", args=[doc.pk]),
            data='{"tree": [], "base_version": 0}', content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["ok"])

    def test_edit_modal_prefills_generated_markdown(self):
        doc = self._make_container_with_record()
        self.client.force_login(self.user)
        resp = self.client.get(reverse("knowledge_base:document_edit", args=[doc.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "## 2026-10-24")
        self.assertContains(resp, "完成了支付回调的自测。")
        # 占位模板文字不再出现
        self.assertNotContains(resp, "记录为本体，此处为同步视图")
