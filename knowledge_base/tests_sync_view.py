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
        KnowledgeNode.objects.create(
            document=doc, uid="r0000000001", parent=None, sort=0,
            text="今天完成了登录模块的自测。", node_type="todo",
            is_done=True, ref_module="daily_record", ref_object_id=1,
            ref_label="每日记录 2026-10-25",
        )
        self.client.force_login(self.user)
        resp = self.client.get(reverse("knowledge_base:document_detail", args=[doc.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "今天完成了登录模块的自测。")
        self.assertContains(resp, "每日记录 2026-10-25")

    def test_normal_document_unaffected(self):
        doc = KnowledgeDocument.objects.create(
            title="普通笔记", category=self.cat, owner=self.user,
            status="published", visibility="private", content="普通内容",
        )
        self.client.force_login(self.user)
        resp = self.client.get(reverse("knowledge_base:document_detail", args=[doc.pk]))
        self.assertEqual(resp.status_code, 200)
        # 模板头部常驻 .kb-sync-nodes 的 CSS 定义，此处断言不应渲染出节点 section
        self.assertNotContains(resp, '<section class="kb-sync-node"')
