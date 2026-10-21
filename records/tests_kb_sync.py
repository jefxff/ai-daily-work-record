"""批次 3 验收：每日记录沉淀到知识库（records.kb_sync）。

口径：记录是本体，知识库里的沉淀是影子——
建/改/删/搬全程跟随，失败不影响每日记录主流程。
"""

from datetime import datetime

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from knowledge_base.models import KnowledgeDocument, KnowledgeNode

from .kb_sync import (REF_MODULE, SOURCE_MARK, apply_flag, sync_batch,
                      sync_record_to_kb, unsync_record)
from .models import DailyRecord


def grant(user, *pairs):
    """按 (module, action) 授权（Permission 行缺失时补建，避免依赖 RBAC 种子迁移）。"""
    from accounts.models import Permission, Role
    from accounts.registry import permission_code

    role = Role.objects.create(code=f"kbsync-test-{user.username}", name="沉淀测试角色")
    for module, action in pairs:
        perm, _ = Permission.objects.get_or_create(
            module=module, action=action,
            defaults={"code": permission_code(module, action), "name": f"{module}-{action}"},
        )
        role.permissions.add(perm)
    role.users.add(user)
    if hasattr(user, "_rbac_codes_cache"):
        del user._rbac_codes_cache


class KbSyncTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("syncer", "s@e.com", "pwd12345", is_active=True)
        grant(self.user, ("daily_record", "read"), ("daily_record", "create"),
              ("daily_record", "update"), ("knowledge", "read"))

    def _record(self, **kw):
        defaults = dict(
            user=self.user, department="质量部",
            work_content="完成 A 客户样品测试", status="完成", progress=100,
            record_date=timezone.make_aware(datetime(2026, 10, 21, 9, 0)),
        )
        defaults.update(kw)
        return DailyRecord.objects.create(**defaults)

    def test_sync_creates_container_and_node(self):
        record = self._record()
        node, container = sync_record_to_kb(record)
        self.assertIsNotNone(node)
        self.assertEqual(node.node_type, "todo")
        self.assertTrue(node.is_done)
        self.assertEqual(node.ref_module, REF_MODULE)
        self.assertEqual(node.ref_object_id, record.pk)
        # 容器带来源指纹（批次 0 修订：定位/清理以 source 为准，不靠标题）
        self.assertEqual(container.source, SOURCE_MARK)
        self.assertEqual(container.visibility, "private")
        self.assertEqual(container.owner, self.user)
        # 记录回填指针
        record.refresh_from_db()
        self.assertEqual(record.kb_doc_id, container.pk)
        self.assertIsNotNone(record.kb_synced_at)

    def test_resync_is_idempotent(self):
        record = self._record()
        sync_record_to_kb(record)
        record.work_content = "完成 A 客户样品测试并出报告"
        node, container = sync_record_to_kb(record)
        self.assertEqual(KnowledgeNode.objects.filter(
            ref_object_id=record.pk, ref_module=REF_MODULE).count(), 1)
        self.assertEqual(node.text, "完成 A 客户样品测试并出报告")
        self.assertEqual(KnowledgeDocument.objects.filter(
            source=SOURCE_MARK, owner=self.user).count(), 1)
        # 同容器重同步同样 bump 版本，挡住开着编辑器的人
        container.refresh_from_db()
        self.assertGreaterEqual(container.tree_version, 2)

    def test_visibility_change_moves_node_and_cleans_old_container(self):
        record = self._record()
        node, private_container = sync_record_to_kb(record)  # 默认私有
        record.kb_visibility = "public"
        node2, public_container = sync_record_to_kb(record)
        self.assertNotEqual(private_container.pk, public_container.pk)
        self.assertEqual(public_container.visibility, "public")
        self.assertEqual(node2.pk, node.pk)  # 搬不是复制
        # 旧容器空了自动清理
        self.assertFalse(KnowledgeDocument.objects.filter(pk=private_container.pk).exists())
        self.assertEqual(KnowledgeNode.objects.filter(
            ref_object_id=record.pk, ref_module=REF_MODULE).count(), 1)

    def test_unsync_removes_node_and_container(self):
        record = self._record()
        sync_record_to_kb(record)
        container = KnowledgeDocument.objects.get(source=SOURCE_MARK)
        self.assertTrue(unsync_record(record))
        self.assertFalse(KnowledgeNode.objects.filter(
            ref_object_id=record.pk, ref_module=REF_MODULE).exists())
        self.assertFalse(KnowledgeDocument.objects.filter(pk=container.pk).exists())
        record.refresh_from_db()
        self.assertIsNone(record.kb_doc_id)

    def test_post_delete_signal_cleans_shadow(self):
        record = self._record()
        sync_record_to_kb(record)
        container_pk = record.kb_doc_id
        record.delete()
        self.assertFalse(KnowledgeNode.objects.filter(
            ref_module=REF_MODULE, ref_object_id=record.pk).exists())
        self.assertFalse(KnowledgeDocument.objects.filter(pk=container_pk).exists())

    def test_apply_flag_disabled_never_creates(self):
        record = self._record()
        apply_flag(record, enabled=False, visibility="private")
        self.assertEqual(KnowledgeDocument.objects.filter(source=SOURCE_MARK).count(), 0)

    def test_sync_never_raises(self):
        """沉淀失败绝不能影响每日记录主流程（吞异常只记日志）。"""
        from unittest import mock

        record = self._record()
        with mock.patch("records.kb_sync._get_or_create_container",
                        side_effect=RuntimeError("boom")):
            node, _ = sync_record_to_kb(record)
            self.assertIsNone(node)
        # 主流程本体毫发无损
        record.refresh_from_db()
        self.assertEqual(record.work_content, "完成 A 客户样品测试")
        self.assertFalse(record.kb_enabled)


class KbSyncViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("syncer2", "s2@e.com", "pwd12345", is_active=True)
        grant(self.user, ("daily_record", "read"), ("daily_record", "create"),
              ("daily_record", "update"), ("knowledge", "read"))
        self.client = Client()
        self.client.force_login(self.user)

    def test_add_view_creates_shadow_when_checked(self):
        resp = self.client.post(reverse("daily_record_add"), {
            "department": "质量部", "work_content": "沉淀我",
            "status": "完成", "progress": "80",
            "kb_enabled": "on", "kb_visibility": "public",
        })
        self.assertIn(resp.status_code, (200, 302))
        record = DailyRecord.objects.get(work_content="沉淀我")
        self.assertTrue(record.kb_enabled)
        self.assertEqual(record.kb_visibility, "public")
        node = KnowledgeNode.objects.get(ref_object_id=record.pk, ref_module=REF_MODULE)
        self.assertEqual(node.document.visibility, "public")

    def test_add_view_without_flag_no_shadow(self):
        self.client.post(reverse("daily_record_add"), {
            "department": "质量部", "work_content": "不沉淀",
            "status": "完成", "progress": "80",
        })
        record = DailyRecord.objects.get(work_content="不沉淀")
        self.assertFalse(record.kb_enabled)
        self.assertEqual(KnowledgeNode.objects.filter(ref_object_id=record.pk).count(), 0)

    def test_edit_view_unchecks_and_removes_shadow(self):
        resp = self.client.post(reverse("daily_record_add"), {
            "department": "质量部", "work_content": "先沉淀",
            "status": "完成", "progress": "80",
            "kb_enabled": "on", "kb_visibility": "private",
        })
        record = DailyRecord.objects.get(work_content="先沉淀")
        self.assertTrue(KnowledgeNode.objects.filter(ref_object_id=record.pk).exists())
        # 取消勾选再保存 → 影子删除
        self.client.post(reverse("daily_record_edit", args=[record.pk]), {
            "department": "质量部", "work_content": "先沉淀",
            "status": "完成", "progress": "90",
        })
        record.refresh_from_db()
        self.assertFalse(record.kb_enabled)
        self.assertEqual(KnowledgeNode.objects.filter(ref_object_id=record.pk).count(), 0)

    def test_batch_endpoint_syncs_unsynced_private_only(self):
        from datetime import datetime as dt

        timezone.make_aware(dt(2026, 10, 20, 9, 0))
        DailyRecord.objects.create(
            user=self.user, department="质量部", work_content="老记录A",
            status="未完成", progress=30,
            record_date=timezone.make_aware(dt(2026, 10, 20, 9, 0)))
        DailyRecord.objects.create(
            user=self.user, department="质量部", work_content="老记录B",
            status="完成", progress=100,
            record_date=timezone.make_aware(dt(2026, 10, 21, 9, 0)))
        # 别人的记录不碰（用户隔离）
        other = User.objects.create_user("other_u", "o@e.com", "pwd12345")
        DailyRecord.objects.create(
            user=other, department="销售部", work_content="别人的",
            status="完成", progress=100)

        resp = self.client.post(reverse("daily_record_kb_sync_batch"))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["synced"], 2)
        # 一律私有（D5）
        for doc in KnowledgeDocument.objects.filter(source=SOURCE_MARK, owner=self.user):
            self.assertEqual(doc.visibility, "private")
        # 未沉淀计数归零
        self.assertEqual(
            DailyRecord.objects.filter(user=self.user, kb_enabled=False).count(), 0)
        # 别人的记录未被沉淀
        self.assertFalse(
            KnowledgeNode.objects.filter(node_type="todo", ref_label__contains="别人的").exists())

    def test_batch_endpoint_get_rejected(self):
        resp = self.client.get(reverse("daily_record_kb_sync_batch"))
        self.assertEqual(resp.status_code, 405)

    def test_form_modal_renders_kb_fields(self):
        resp = self.client.get(reverse("daily_record_add"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn('name="kb_enabled"', body)
        self.assertIn('name="kb_visibility"', body)
