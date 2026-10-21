"""幕布化 4b 验收：整树保存 PUT / 乐观锁 409 / 待办转写 / 编辑器页权限。

验收口径（方案 03 第五节 + 开发计划书批次 4b）：
- PUT 保存 round-trip：树入库、content 兼容层同步写回、version+1、editor_mode 转 outline；
- base_version 过期 → 409 带最新版本；
- 节点上限 1000 / 深度上限 20 → 400；
- 待办转写：日期前缀 → due_date，content 中写回前缀（信息不丢、round-trip 幂等）；
- 权限：owner 专属编辑，他人公开文档只读 403，他人私有文档 404 不泄露存在性；
- 可见性切换 → tree_version 自增（编辑器下次保存感知 409）。
"""
import json
from datetime import date

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from records.models import OperationLog

from . import access as kb_access
from .models import KnowledgeDocument, KnowledgeNode
from .outline import md_to_tree, tree_to_md

PWD = 'X12345678'


def grant(user, *pairs):
    from .tests import grant as _grant
    return _grant(user, *pairs)


def _put(client, url, payload):
    return client.put(url, data=json.dumps(payload), content_type='application/json')


class OutlineSaveTests(TestCase):
    """PUT 整树保存：落库 / 乐观锁 / 上限 / 待办转写 / 留痕。"""

    def setUp(self):
        self.alice = User.objects.create_user('ed_alice', 'a@e.com', PWD, is_active=True)
        self.bob = User.objects.create_user('ed_bob', 'b@e.com', PWD, is_active=True)
        grant(self.alice, ('knowledge', 'read'), ('knowledge', 'update'))
        grant(self.bob, ('knowledge', 'read'), ('knowledge', 'update'))
        self.doc = KnowledgeDocument.objects.create(
            title='大纲测试', content='- 旧内容', status='published',
            owner=self.alice, visibility='public')
        self.url = reverse('knowledge_base:api_doc_tree', args=[self.doc.pk])
        self.c = Client()
        self.c.force_login(self.alice)

    def _tree(self):
        self.doc.refresh_from_db()
        nodes = list(self.doc.nodes.order_by('sort', 'id'))
        by_parent = {}
        for n in nodes:
            by_parent.setdefault(n.parent_id, []).append(n)

        def build(parent_id):
            return [(n, build(n.pk)) for n in by_parent.get(parent_id, [])]
        return build(None)

    def test_put_round_trip(self):
        resp = _put(self.c, self.url, {
            'base_version': 1,
            'tree': [{'uid': 'aaa111bbb222', 'text': '根节点',
                      'children': [{'uid': 'ccc333ddd444', 'text': '子节点'}]},
                     {'uid': 'ddd444eee555', 'text': '第二个根'}],
        })
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['version'], 2)

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.editor_mode, 'outline')
        self.assertEqual(self.doc.tree_version, 2)
        self.assertEqual(self.doc.content, '- 根节点\n  - 子节点\n- 第二个根')
        tree = self._tree()
        self.assertEqual(len(tree), 2)
        self.assertEqual(tree[0][0].text, '根节点')
        self.assertEqual(len(tree[0][1]), 1)
        self.assertEqual(tree[0][1][0][0].text, '子节点')
        self.assertEqual(tree[0][1][0][0].parent_id, tree[0][0].pk)

    def test_existing_uid_reused_and_fields_updated(self):
        _put(self.c, self.url, {'base_version': 1, 'tree': [
            {'uid': 'aaa111bbb222', 'text': '甲'}]})
        _put(self.c, self.url, {'base_version': 2, 'tree': [
            {'uid': 'aaa111bbb222', 'type': 'todo', 'text': '甲改', 'done': True}]})
        self.assertEqual(KnowledgeNode.objects.count(), 1)
        node = KnowledgeNode.objects.get()
        self.assertEqual(node.text, '甲改')
        self.assertEqual(node.node_type, 'todo')
        self.assertTrue(node.is_done)

    def test_removed_nodes_deleted(self):
        _put(self.c, self.url, {'base_version': 1, 'tree': [
            {'uid': 'aaa111bbb222', 'text': '甲'},
            {'uid': 'ccc333ddd444', 'text': '乙'}]})
        _put(self.c, self.url, {'base_version': 2, 'tree': [
            {'uid': 'ccc333ddd444', 'text': '乙保留'}]})
        uids = set(KnowledgeNode.objects.values_list('uid', flat=True))
        self.assertEqual(uids, {'ccc333ddd444'})

    def test_conflict_returns_409_with_latest_version(self):
        _put(self.c, self.url, {'base_version': 1, 'tree': [{'text': '第一版'}]})
        resp = _put(self.c, self.url, {'base_version': 1, 'tree': [{'text': '过期版'}]})
        self.assertEqual(resp.status_code, 409)
        data = resp.json()
        self.assertEqual(data['error'], 'conflict')
        self.assertEqual(data['version'], 2)

    def test_node_limit_1000(self):
        tree = [{'text': f'节点{i}'} for i in range(1001)]
        resp = _put(self.c, self.url, {'base_version': 1, 'tree': tree})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(KnowledgeNode.objects.exists())

    def test_depth_limit_20(self):
        node = {'text': '叶'}
        for _ in range(21):
            node = {'text': f'层', 'children': [node]}
        resp = _put(self.c, self.url, {'base_version': 1, 'tree': [node]})
        self.assertEqual(resp.status_code, 400)

    def test_todo_date_prefix_converted(self):
        resp = _put(self.c, self.url, {'base_version': 1, 'tree': [
            {'type': 'todo', 'text': '2026-10-25 交报告'}]})
        self.assertEqual(resp.status_code, 200)
        node = KnowledgeNode.objects.get()
        self.assertEqual(node.due_date, date(2026, 10, 25))
        self.assertEqual(node.text, '交报告')
        self.assertFalse(node.is_done)
        # content 兼容层写回日期前缀：信息不丢
        self.doc.refresh_from_db()
        self.assertIn('- [ ] 2026-10-25 交报告', self.doc.content)

    def test_todo_due_field_direct(self):
        _put(self.c, self.url, {'base_version': 1, 'tree': [
            {'type': 'todo', 'text': '事项', 'due': '2026-11-01'}]})
        node = KnowledgeNode.objects.get()
        self.assertEqual(node.due_date, date(2026, 11, 1))
        self.assertEqual(node.text, '事项')

    def test_todo_due_cleared_and_invalid_ignored(self):
        _put(self.c, self.url, {'base_version': 1, 'tree': [
            {'type': 'todo', 'text': '事项', 'due': '2026-11-01'}]})
        _put(self.c, self.url, {'base_version': 2, 'tree': [
            {'type': 'todo', 'text': '事项', 'due': None}]})
        self.assertIsNone(KnowledgeNode.objects.get().due_date)
        _put(self.c, self.url, {'base_version': 3, 'tree': [
            {'type': 'todo', 'text': '事项', 'due': '2026-13-99'}]})
        self.assertIsNone(KnowledgeNode.objects.get().due_date)

    def test_non_todo_due_dropped(self):
        _put(self.c, self.url, {'base_version': 1, 'tree': [
            {'type': 'text', 'text': '普通', 'due': '2026-11-01'}]})
        self.assertIsNone(KnowledgeNode.objects.get().due_date)

    def test_missing_uid_assigned_server_side(self):
        resp = _put(self.c, self.url, {'base_version': 1, 'tree': [{'text': '无uid'}]})
        self.assertEqual(resp.status_code, 200)
        uid = KnowledgeNode.objects.get().uid
        self.assertEqual(len(uid), 12)

    def test_get_tree_includes_due(self):
        _put(self.c, self.url, {'base_version': 1, 'tree': [
            {'type': 'todo', 'text': '2026-10-25 交报告'}]})
        resp = self.c.get(self.url)
        node = resp.json()['tree'][0]
        self.assertEqual(node['due'], '2026-10-25')
        self.assertEqual(node['text'], '交报告')

    def test_operation_log_written(self):
        _put(self.c, self.url, {'base_version': 1, 'tree': [{'text': '甲'}]})
        log = OperationLog.objects.filter(
            module_type='知识库', operation_type='大纲保存').order_by('-id').first()
        self.assertIsNotNone(log)
        self.assertIn('大纲测试', log.change_summary)

    def test_bad_json_400(self):
        resp = self.c.put(self.url, data='not-json', content_type='application/json')
        self.assertEqual(resp.status_code, 400)


class OutlineSavePermissionTests(TestCase):
    """写权限：owner 专属；公开他人只读；私有他人 404 不泄露。"""

    def setUp(self):
        self.alice = User.objects.create_user('edp_alice', 'a@e.com', PWD, is_active=True)
        self.bob = User.objects.create_user('edp_bob', 'b@e.com', PWD, is_active=True)
        grant(self.alice, ('knowledge', 'read'), ('knowledge', 'update'))
        grant(self.bob, ('knowledge', 'read'), ('knowledge', 'update'))
        self.pub = KnowledgeDocument.objects.create(
            title='公开文档', content='x', status='published',
            owner=self.alice, visibility='public')
        self.priv = KnowledgeDocument.objects.create(
            title='Alice私人', content='x', status='published',
            owner=self.alice, visibility='private')

    def _put(self, doc, payload=None):
        c = Client()
        c.force_login(self.bob)
        return _put(c, reverse('knowledge_base:api_doc_tree', args=[doc.pk]),
                    payload or {'base_version': 1, 'tree': [{'text': '改'}]})

    def test_public_doc_others_readonly_403(self):
        resp = self._put(self.pub)
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(KnowledgeNode.objects.exists())

    def test_private_doc_others_404(self):
        resp = self._put(self.priv)
        self.assertEqual(resp.status_code, 404)

    def test_owner_can_save_private(self):
        c = Client()
        c.force_login(self.alice)
        resp = _put(c, reverse('knowledge_base:api_doc_tree', args=[self.priv.pk]),
                    {'base_version': 1, 'tree': [{'text': '自己的'}]})
        self.assertEqual(resp.status_code, 200)

    def test_readonly_user_put_403(self):
        carol = User.objects.create_user('edp_carol', 'c@e.com', PWD, is_active=True)
        grant(carol, ('knowledge', 'read'))
        c = Client()
        c.force_login(carol)
        resp = _put(c, reverse('knowledge_base:api_doc_tree', args=[self.pub.pk]),
                    {'base_version': 1, 'tree': [{'text': 'x'}]})
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_put_redirects(self):
        resp = Client().put(reverse('knowledge_base:api_doc_tree', args=[self.pub.pk]),
                            data='{}', content_type='application/json')
        self.assertEqual(resp.status_code, 302)


class OutlineEditorPageTests(TestCase):
    """编辑器页：owner 可进，他人公开文档跳详情，私有 404。"""

    def setUp(self):
        self.alice = User.objects.create_user('edg_alice', 'a@e.com', PWD, is_active=True)
        self.bob = User.objects.create_user('edg_bob', 'b@e.com', PWD, is_active=True)
        grant(self.alice, ('knowledge', 'read'), ('knowledge', 'update'))
        grant(self.bob, ('knowledge', 'read'), ('knowledge', 'update'))
        self.pub = KnowledgeDocument.objects.create(
            title='公开文档', content='x', status='published',
            owner=self.alice, visibility='public')
        self.priv = KnowledgeDocument.objects.create(
            title='Alice私人', content='x', status='published',
            owner=self.alice, visibility='private')

    def _get(self, doc, user):
        c = Client()
        c.force_login(user)
        return c.get(reverse('knowledge_base:outline_editor', args=[doc.pk]))

    def test_owner_gets_editor(self):
        resp = self._get(self.pub, self.alice)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'kb-editor-data')
        self.assertContains(resp, 'kbEdRows')

    def test_others_public_doc_redirect_to_detail(self):
        resp = self._get(self.pub, self.bob)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse('knowledge_base:document_detail',
                                           args=[self.pub.pk]))

    def test_others_private_doc_404(self):
        resp = self._get(self.priv, self.bob)
        self.assertEqual(resp.status_code, 404)

    def test_detail_page_shows_entry_only_for_owner(self):
        c = Client()
        c.force_login(self.alice)
        resp = c.get(reverse('knowledge_base:document_detail', args=[self.pub.pk]))
        self.assertContains(resp, '编辑大纲')
        c2 = Client()
        c2.force_login(self.bob)
        resp2 = c2.get(reverse('knowledge_base:document_detail', args=[self.pub.pk]))
        self.assertNotContains(resp2, '编辑大纲')


class VisibilityBumpVersionTests(TestCase):
    """可见性切换 → tree_version 自增：编辑器下次保存感知 409。"""

    def setUp(self):
        self.alice = User.objects.create_user('edv_alice', 'a@e.com', PWD, is_active=True)
        self.doc = KnowledgeDocument.objects.create(
            title='版本联动', content='x', owner=self.alice, visibility='private')
        self.v0 = self.doc.tree_version

    def test_bump_on_public_and_revoke(self):
        kb_access.set_visibility(self.doc, kb_access.PUBLIC)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.tree_version, self.v0 + 1)
        kb_access.set_visibility(self.doc, kb_access.PRIVATE)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.tree_version, self.v0 + 2)

    def test_no_change_no_bump(self):
        self.assertFalse(kb_access.set_visibility(self.doc, kb_access.PRIVATE))
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.tree_version, self.v0)


class TodoDueRoundTripTests(TestCase):
    """tree_to_md 待办 due 写回：与 md_to_tree 对接后 round-trip 幂等。"""

    def test_due_written_back(self):
        md = tree_to_md([{'type': 'todo', 'text': '交报告', 'due': '2026-10-25',
                          'done': False, 'children': []}])
        self.assertEqual(md, '- [ ] 2026-10-25 交报告')

    def test_no_double_prefix(self):
        md = tree_to_md([{'type': 'todo', 'text': '2026-10-25 交报告', 'due': '2026-10-25',
                          'done': True, 'children': []}])
        self.assertEqual(md, '- [x] 2026-10-25 交报告')

    def test_round_trip_idempotent_with_due(self):
        md1 = tree_to_md([{'type': 'todo', 'text': '交报告', 'due': '2026-10-25',
                           'done': False, 'children': []}])
        # md_to_tree 不剥离前缀（保存时服务端转写），文本等价、逐字节幂等
        md2 = tree_to_md(md_to_tree(md1))
        self.assertEqual(md1, md2)
