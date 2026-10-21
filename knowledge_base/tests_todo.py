"""幕布化批次 5 验收：知识待办 → 工作台聚合 + 到期提醒。

验收口径（开发计划书批次 5）：
- 工作台 collect_todos 新增 knowledge_todo 来源：仅聚合当前用户可见文档
  （公开+本人）的未完成待办，他人私有文档绝不出现；
- 链接直达编辑器并定位节点（?node=<uid>）；
- 到期提醒走 notifications 既有进程内调度（ScheduledTask + notify_knowledge_todo
  命令 + knowledge_todo_due 模板），dedup 幂等，不建外部计划任务。
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from notifications.models import Notification, NotificationTemplate, ScheduledTask
from notifications.scheduler import COMMAND_PARAMS

from .models import KnowledgeDocument, KnowledgeNode

PWD = 'X12345678'


def _mk_doc(owner, visibility='private'):
    return KnowledgeDocument.objects.create(
        title='待办聚合测试', content='- x', status='published',
        owner=owner, visibility=visibility)


def _mk_todo(doc, text='事项', due=None, is_done=False, uid='aaaaaaaaaaaa',
             node_type='todo'):
    return KnowledgeNode.objects.create(
        document=doc, uid=uid, parent=None, sort=0, text=text,
        node_type=node_type, is_done=is_done, due_date=due)


class WorkspaceKnowledgeTodoTests(TestCase):
    """工作台聚合：可见性 + 窗口 + 链接定位。"""

    def setUp(self):
        self.alice = User.objects.create_user('wt_alice', 'a@e.com', PWD, is_active=True)
        self.bob = User.objects.create_user('wt_bob', 'b@e.com', PWD, is_active=True)
        from .tests import grant
        grant(self.alice, ('knowledge', 'read'))
        grant(self.bob, ('knowledge', 'read'))
        self.today = timezone.localdate()

    def _collect(self, user, **kw):
        from records.views_workspace import collect_todos
        return collect_todos(user, today=self.today, **kw)

    def test_owner_sees_overdue_todo_with_node_link(self):
        doc = _mk_doc(self.alice)
        _mk_todo(doc, text='2026 交报告', due=self.today - timedelta(days=2),
                 uid='111111111111')
        todos = self._collect(self.alice, source_filter='knowledge_todo')
        self.assertEqual(len(todos), 1)
        row = todos[0]
        self.assertEqual(row['urgency'], '高')
        self.assertEqual(row['source'], 'knowledge_todo')
        self.assertEqual(row['status'], '已逾期')
        self.assertIn(f'/knowledge/document/{doc.pk}/outline/?node=111111111111',
                      row['url'])

    def test_private_todo_hidden_from_others(self):
        doc = _mk_doc(self.alice)
        _mk_todo(doc, due=self.today, uid='222222222222')
        self.assertEqual(self._collect(self.bob, source_filter='knowledge_todo'), [])

    def test_public_todo_visible_to_others(self):
        doc = _mk_doc(self.alice, visibility='public')
        _mk_todo(doc, due=self.today, uid='333333333333')
        todos = self._collect(self.bob, source_filter='knowledge_todo')
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]['urgency'], '中')
        self.assertEqual(todos[0]['status'], '进行中')

    def test_done_and_no_due_and_far_future_excluded(self):
        doc = _mk_doc(self.alice)
        _mk_todo(doc, due=self.today, is_done=True, uid='444444444444')
        _mk_todo(doc, due=None, uid='555555555555')
        _mk_todo(doc, due=self.today + timedelta(days=8), uid='666666666666')
        _mk_todo(doc, text='普通行', due=self.today, uid='777777777777',
                 node_type='text')  # 非 todo 节点不入聚合
        self.assertEqual(self._collect(self.alice, source_filter='knowledge_todo'), [])

    def test_without_knowledge_perm_no_todo(self):
        carol = User.objects.create_user('wt_carol', 'c@e.com', PWD, is_active=True)
        doc = _mk_doc(self.alice, visibility='public')
        _mk_todo(doc, due=self.today, uid='888888888888')
        self.assertEqual(self._collect(carol, source_filter='knowledge_todo'), [])


class NotifyKnowledgeTodoTests(TestCase):
    """到期提醒命令：窗口 / 幂等 / 模板 / 链接 / 排除项。"""

    def setUp(self):
        self.alice = User.objects.create_user('nt_alice', 'a@e.com', PWD, is_active=True)
        self.today = timezone.localdate()
        self.doc = _mk_doc(self.alice)

    def _run(self, **opts):
        from django.core.management import call_command
        import io
        out = io.StringIO()
        call_command('notify_knowledge_todo', stdout=out, **opts)
        return out.getvalue()

    def test_template_and_task_seeded(self):
        self.assertTrue(NotificationTemplate.objects.filter(
            code='knowledge_todo_due', is_active=True).exists())
        task = ScheduledTask.objects.filter(code='notify_knowledge_todo').first()
        self.assertIsNotNone(task)
        self.assertTrue(task.enabled)
        self.assertIn('notify_knowledge_todo', COMMAND_PARAMS)

    def test_owner_notified_with_dedup(self):
        _mk_todo(self.doc, text='交样品', due=self.today, uid='111111111111')
        self._run()
        n = Notification.objects.filter(recipient=self.alice).latest('id')
        self.assertEqual(n.link, f'/knowledge/document/{self.doc.pk}/outline/?node=111111111111')
        self.assertIn('交样品', n.content)
        # 同日重跑：dedup 幂等不重复
        self._run()
        self.assertEqual(Notification.objects.filter(recipient=self.alice).count(), 1)

    def test_excludes_done_no_due_owner_missing(self):
        _mk_todo(self.doc, due=self.today, is_done=True, uid='222222222222')
        _mk_todo(self.doc, due=None, uid='333333333333')
        orphan_doc = KnowledgeDocument.objects.create(
            title='孤儿', content='x', status='published', owner=None)
        _mk_todo(orphan_doc, due=self.today, uid='444444444444')
        self._run()
        self.assertEqual(Notification.objects.filter(recipient=self.alice).count(), 0)

    def test_window_boundaries(self):
        _mk_todo(self.doc, due=self.today - timedelta(days=8),
                 uid='555555555555')  # 超出回溯窗口
        _mk_todo(self.doc, due=self.today + timedelta(days=4),
                 uid='666666666666')  # 超出提前窗口（默认 3）
        _mk_todo(self.doc, due=self.today - timedelta(days=7),
                 uid='777777777777')  # 回溯边界内
        self._run()
        self.assertEqual(Notification.objects.filter(recipient=self.alice).count(), 1)

    def test_overdue_days_param_extends_window(self):
        _mk_todo(self.doc, due=self.today - timedelta(days=8),
                 uid='888888888888')
        self._run(overdue_days=10)
        self.assertEqual(Notification.objects.filter(recipient=self.alice).count(), 1)
