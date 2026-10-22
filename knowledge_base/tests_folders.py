"""文件夹树验收（批次6 / 方案04 第九节 10 条清单原样执行）。

- 隔离：文件夹按 owner 隔离，「不存在」与「别人的」同一文案，不暴露 ID 空间；
- 环检测：拖文件夹进自己/子孙 → 服务端 400；
- 删除 = 内容上提：子文件夹与文档上提一层；顶层删除后变「未归档」，篇数不变；
- 归档权限：他人不能动别人的文档（403），自己的文档不能塞进别人的文件夹（400）；
- 可见性与文件夹正交：公开文档入私有文件夹后，他人公开列表仍可见；
- 沉淀容器落「未归档」：kb_sync 不感知文件夹（代码零改动）。
"""
import json

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from . import folders as kb_folders
from .models import KnowledgeDocument, KnowledgeFolder

PWD = 'X12345678'


def grant(user, *pairs):
    from .tests import grant as _grant
    return _grant(user, *pairs)


def _post(client, url, payload=None):
    return client.post(url, data=json.dumps(payload or {}), content_type='application/json')


class FolderApiTests(TestCase):
    """方案04 第九节：验收 1/2/3/4/7 + 树隔离。"""

    def setUp(self):
        self.alice = User.objects.create_user('ft_alice', 'a@e.com', PWD, is_active=True)
        self.bob = User.objects.create_user('ft_bob', 'b@e.com', PWD, is_active=True)
        grant(self.alice, ('knowledge', 'read'), ('knowledge', 'update'))
        grant(self.bob, ('knowledge', 'read'), ('knowledge', 'update'))
        self.c = Client()
        self.c.force_login(self.alice)
        self.url_new = reverse('knowledge_base:api_folders')

    # —— 验收 1：新建 8 层嵌套成功；第 9 层报「最多 8 层」 ——
    def test_depth_limit(self):
        parent_id = None
        chain = []
        for i in range(kb_folders.MAX_DEPTH):          # 1..8 层全部成功
            resp = _post(self.c, self.url_new, {'name': f'L{i + 1}', 'parent_id': parent_id})
            self.assertEqual(resp.status_code, 200, resp.content)
            data = resp.json()
            self.assertTrue(data['ok'])
            parent_id = data['id']
            chain.append(parent_id)
        resp = _post(self.c, self.url_new, {'name': 'L9', 'parent_id': parent_id})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('最多 8 层', resp.json()['message'])
        self.assertFalse(KnowledgeFolder.objects.filter(name='L9').exists())

    # —— 验收 2：拖文件夹进自己的子孙 → 服务端 400 ——
    def test_move_into_descendant_rejected(self):
        root = kb_folders.create_folder(self.alice, '根')
        child = kb_folders.create_folder(self.alice, '子', root.pk)
        url = reverse('knowledge_base:api_folder_move', args=[root.pk])
        resp = _post(self.c, url, {'parent_id': child.pk})
        self.assertEqual(resp.status_code, 400)
        root.refresh_from_db()
        self.assertIsNone(root.parent)                 # 无副作用
        # 移到自己同样拒绝
        resp = _post(self.c, url, {'parent_id': root.pk})
        self.assertEqual(resp.status_code, 400)
        # 无关分支可自由移动
        other = kb_folders.create_folder(self.alice, '其他')
        resp = _post(self.c, url, {'parent_id': other.pk})
        self.assertEqual(resp.status_code, 200)
        root.refresh_from_db()
        self.assertEqual(root.parent_id, other.pk)

    # —— 验收 3：删除有内容的文件夹 → 内容上提一层，篇数不变 ——
    def test_delete_lifts_children(self):
        root = kb_folders.create_folder(self.alice, '删除我')
        sub = kb_folders.create_folder(self.alice, '子夹', root.pk)
        doc = KnowledgeDocument.objects.create(
            title='在删除夹里', content='x', status='published',
            owner=self.alice, visibility='public', folder=root)
        top = KnowledgeFolder.objects.count()          # 删除前文件夹总数
        resp = _post(self.c, reverse('knowledge_base:api_folder_delete', args=[root.pk]), {})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['moved_docs'], 1)
        doc.refresh_from_db()
        self.assertIsNone(doc.folder_id)               # 文档上提到顶层 → 未归档
        sub.refresh_from_db()
        self.assertIsNone(sub.parent_id)               # 子文件夹上提一层
        self.assertEqual(KnowledgeFolder.objects.count(), top - 1)
        self.assertTrue(KnowledgeDocument.objects.filter(pk=doc.pk).exists())

    # —— 验收 4：删除顶层文件夹 → 内容全部出现在「未归档」 ——
    def test_delete_top_level_to_unarchived(self):
        top = kb_folders.create_folder(self.alice, '顶层夹')
        d1 = KnowledgeDocument.objects.create(
            title='文档一', content='x', status='published',
            owner=self.alice, visibility='private', folder=top)
        d2 = KnowledgeDocument.objects.create(
            title='文档二', content='x', status='published',
            owner=self.alice, visibility='public', folder=top)
        resp = _post(self.c, reverse('knowledge_base:api_folder_delete', args=[top.pk]), {})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['moved_docs'], 2)
        d1.refresh_from_db(); d2.refresh_from_db()
        self.assertIsNone(d1.folder_id)
        self.assertIsNone(d2.folder_id)
        tree = kb_folders.folder_tree(self.alice)
        ids = {d['id'] for d in tree['unarchived']}
        self.assertIn(d1.pk, ids)
        self.assertIn(d2.pk, ids)

    # —— 验收 7：访问别人的文件夹接口 → 与「不存在」完全同响应 ——
    def test_other_users_folder_not_distinguished_from_missing(self):
        alice_folder = kb_folders.create_folder(self.alice, 'alice的')
        for name, url_tpl in [
            ('rename', 'knowledge_base:api_folder_rename'),
            ('move', 'knowledge_base:api_folder_move'),
            ('delete', 'knowledge_base:api_folder_delete'),
        ]:
            cb = Client()
            cb.force_login(self.bob)
            missing = reverse(url_tpl, args=[999999])
            theirs = reverse(url_tpl, args=[alice_folder.pk])
            payload = {'name': 'x', 'parent_id': None}
            r_missing = _post(cb, missing, payload)
            r_theirs = _post(cb, theirs, payload)
            self.assertEqual(r_missing.status_code, r_theirs.status_code, name)
            self.assertEqual(r_missing.json(), r_theirs.json(), name)

    # —— 树隔离：bob 的树不含 alice 的文件夹 ——
    def test_tree_isolated_by_owner(self):
        kb_folders.create_folder(self.alice, 'alice专属')
        cb = Client()
        cb.force_login(self.bob)
        resp = cb.get(self.url_new)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        names = [n['name'] for n in data['tree']]
        self.assertNotIn('alice专属', names)


class DocMoveTests(TestCase):
    """方案04 第九节：验收 5/6/8/9。"""

    def setUp(self):
        self.alice = User.objects.create_user('dm_alice', 'a@e.com', PWD, is_active=True)
        self.bob = User.objects.create_user('dm_bob', 'b@e.com', PWD, is_active=True)
        grant(self.alice, ('knowledge', 'read'), ('knowledge', 'update'))
        grant(self.bob, ('knowledge', 'read'), ('knowledge', 'update'))
        self.c = Client()
        self.c.force_login(self.alice)

    # —— 验收 5：拖别人文档进自己文件夹 → 403，无副作用 ——
    def test_move_others_doc_forbidden(self):
        alice_doc = KnowledgeDocument.objects.create(
            title='alice的公开文档', content='x', status='published',
            owner=self.alice, visibility='public')
        bob_folder = KnowledgeFolder.objects.create(name='bob的夹', owner=self.bob)
        cb = Client()
        cb.force_login(self.bob)
        resp = _post(cb, reverse('knowledge_base:api_doc_move', args=[alice_doc.pk]),
                     {'folder_id': bob_folder.pk})
        self.assertEqual(resp.status_code, 403)
        alice_doc.refresh_from_db()
        self.assertIsNone(alice_doc.folder_id)

    # —— 验收 6：自己的文档拖进别人文件夹 → 400「文件夹不存在或没有权限」 ——
    def test_doc_into_others_folder_rejected(self):
        alice_doc = KnowledgeDocument.objects.create(
            title='alice的文档', content='x', status='published',
            owner=self.alice, visibility='private')
        bob_folder = KnowledgeFolder.objects.create(name='bob的夹', owner=self.bob)
        resp = _post(self.c, reverse('knowledge_base:api_doc_move', args=[alice_doc.pk]),
                     {'folder_id': bob_folder.pk})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['message'], kb_folders.NOT_FOUND)
        alice_doc.refresh_from_db()
        self.assertIsNone(alice_doc.folder_id)

    # —— 验收 8：未归档文档：侧栏可见、可拖入文件夹、详情页路径「未归档」 ——
    def test_unarchived_visible_and_movable(self):
        doc = KnowledgeDocument.objects.create(
            title='未归档文档', content='x', status='published',
            owner=self.alice, visibility='private')
        tree = kb_folders.folder_tree(self.alice)
        self.assertIn(doc.pk, [d['id'] for d in tree['unarchived']])
        # 拖入文件夹
        folder = kb_folders.create_folder(self.alice, '目标夹')
        resp = _post(self.c, reverse('knowledge_base:api_doc_move', args=[doc.pk]),
                     {'folder_id': folder.pk})
        self.assertEqual(resp.status_code, 200)
        doc.refresh_from_db()
        self.assertEqual(doc.folder_id, folder.pk)
        # 拖回未归档（folder_id=0/null 语义）
        resp = _post(self.c, reverse('knowledge_base:api_doc_move', args=[doc.pk]),
                     {'folder_id': '0'})
        self.assertEqual(resp.status_code, 200)
        doc.refresh_from_db()
        self.assertIsNone(doc.folder_id)
        # 详情页：未归档 → 面包屑显示「未归档」（folder_breadcrumbs 为空）
        resp = self.c.get(reverse('knowledge_base:document_detail', args=[doc.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '未归档')

    # —— 验收 9：公开文档拖入私有文件夹 → 他人公开列表仍可见 ——
    def test_visibility_orthogonal_to_folder(self):
        doc = KnowledgeDocument.objects.create(
            title='公开但已归档', content='x', status='published',
            owner=self.alice, visibility='public')
        folder = kb_folders.create_folder(self.alice, 'alice私有夹')
        resp = _post(self.c, reverse('knowledge_base:api_doc_move', args=[doc.pk]),
                     {'folder_id': folder.pk})
        self.assertEqual(resp.status_code, 200)
        cb = Client()
        cb.force_login(self.bob)
        resp = cb.get(reverse('knowledge_base:document_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '公开但已归档')


class FolderFilterTests(TestCase):
    """列表页 ?folder= 过滤 + 沉淀容器落未归档（验收 10）。"""

    def setUp(self):
        self.alice = User.objects.create_user('ff_alice', 'a@e.com', PWD, is_active=True)
        grant(self.alice, ('knowledge', 'read'))
        self.c = Client()
        self.c.force_login(self.alice)

    def test_list_filter_by_folder_and_unarchived(self):
        folder = kb_folders.create_folder(self.alice, '过滤夹')
        KnowledgeDocument.objects.create(
            title='夹内文档', content='x', status='published',
            owner=self.alice, visibility='public', folder=folder)
        KnowledgeDocument.objects.create(
            title='未归档文档', content='x', status='published',
            owner=self.alice, visibility='public')
        KnowledgeDocument.objects.create(
            title='alice私有', content='x', status='published',
            owner=self.alice, visibility='private', folder=folder)

        def titles(resp):
            # 主体列表断言走 context（侧栏树本来就展示本人全部文档标题，
            # 用 assertNotContains 会被侧栏误伤）
            return {d.title for d in resp.context['documents'].object_list}

        resp = self.c.get(reverse('knowledge_base:document_list') + f'?folder={folder.pk}')
        self.assertEqual(titles(resp), {'夹内文档', 'alice私有'})

        resp = self.c.get(reverse('knowledge_base:document_list') + '?folder=0')
        self.assertEqual(titles(resp), {'未归档文档'})

        # 别人的文件夹 id：忽略不泄露，回落全量视图（主体含全部三篇）
        bob_folder = KnowledgeFolder.objects.create(name='bob的夹', owner=self.bob_user())
        resp = self.c.get(reverse('knowledge_base:document_list') + f'?folder={bob_folder.pk}')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(titles(resp), {'夹内文档', '未归档文档', 'alice私有'})
        self.assertNotContains(resp, 'bob的夹')

    def bob_user(self):
        user, _ = User.objects.get_or_create(
            username='ff_bob', defaults={'email': 'b@e.com', 'is_active': True})
        return user

    # —— 验收 10：沉淀容器落「未归档」（kb_sync 不感知文件夹，代码零改动） ——
    def test_kb_sync_container_unarchived(self):
        from records.kb_sync import _get_or_create_container
        from records.models import DailyRecord

        record = DailyRecord.objects.create(
            user=self.alice, department='测试部',
            work_content='沉淀测试', status='完成')
        container, created = _get_or_create_container(self.alice, record, 'private')
        self.assertTrue(created)
        self.assertIsNone(container.folder_id)

    def test_breadcrumbs(self):
        root = kb_folders.create_folder(self.alice, '一级')
        child = kb_folders.create_folder(self.alice, '二级', root.pk)
        doc = KnowledgeDocument.objects.create(
            title='路径文档', content='x', status='published',
            owner=self.alice, visibility='public', folder=child)
        chain = kb_folders.breadcrumbs(doc.folder_id, self.alice)
        self.assertEqual([c['name'] for c in chain], ['一级', '二级'])
        # 他人的文件夹 → 空（详情页回落「未归档」，不泄露路径）
        self.assertEqual(kb_folders.breadcrumbs(doc.folder_id, self.bob_user()), [])
        # 未归档 → 空
        self.assertEqual(kb_folders.breadcrumbs(None, self.alice), [])
