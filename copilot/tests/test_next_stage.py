"""下一阶段（B1 可信）的回归测试。

覆盖四块：
- 反馈闭环：幂等、越权、原因白名单、菜鸟转用例（copilot_promote）；
- 会话管理：列表/改名/软删，删除后 latest 顺延；
- 保留策略：copilot_prune 默认 dry-run、--confirm 才删；
- 权限一致性：撤权后历史回答隐去正文，复权后恢复。
"""

import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Permission, Role
from copilot.models import (CopilotConversation, CopilotEgressLog, CopilotEvalCase,
                            CopilotFeedback, CopilotMessage, CopilotPolicy,
                            CopilotRun, CopilotToolCall)

User = get_user_model()
LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


def _grant(user, *codes):
    """给用户建一个临时角色并挂上权限码（模块.动作）。"""
    role, _ = Role.objects.get_or_create(code=f'role_{user.username}', defaults={'name': '测试角色'})
    for code in codes:
        module, action = code.split('.')
        perm, _ = Permission.objects.get_or_create(
            code=code, defaults={'module': module, 'action': action, 'name': code})
        role.permissions.add(perm)
    role.users.add(user)
    return role


class BaseCase(TestCase):
    def setUp(self):
        cache.clear()
        CopilotPolicy.objects.all().delete()
        CopilotPolicy.objects.create(rate_limit_per_10min=100, rate_limit_per_day=1000)
        self.user = User.objects.create_superuser('stage_admin', 's@x.com', 'p')
        self.client.force_login(self.user)
        self.conversation = CopilotConversation.objects.create(
            user=self.user, title='客户客诉有哪些')
        self.question = CopilotMessage.objects.create(
            conversation=self.conversation, role='user', content='华建建材的客诉有哪些')
        self.answer = CopilotMessage.objects.create(
            conversation=self.conversation, role='assistant',
            content='华建建材有 1 条客诉。',
            refs=[{'module': 'customer_complaint', 'label': '华建建材', 'id': 1}])

    def post_json(self, name, payload, **kwargs):
        return self.client.post(
            reverse(name), data=json.dumps(payload),
            content_type='application/json', **kwargs)


class FeedbackTests(BaseCase):
    def test_vote_is_idempotent_per_message_and_user(self):
        first = self.post_json('copilot:feedback',
                               {'message_id': self.answer.pk, 'vote': -1})
        self.assertEqual(first.status_code, 200)
        second = self.post_json('copilot:feedback',
                                {'message_id': self.answer.pk, 'vote': 1})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(CopilotFeedback.objects.count(), 1)
        self.assertEqual(CopilotFeedback.objects.get().vote, 1)

    def test_reason_whitelist(self):
        bad = self.post_json('copilot:feedback',
                             {'message_id': self.answer.pk, 'vote': -1,
                              'reason': '随便写的'})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()['error'], 'BAD_REASON')
        ok = self.post_json('copilot:feedback',
                            {'message_id': self.answer.pk, 'vote': -1,
                             'reason': 'wrong_number'})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(CopilotFeedback.objects.get().reason, 'wrong_number')

    def test_cannot_vote_on_someone_elses_message(self):
        other = User.objects.create_user('fb_other', password='p')
        _grant(other, 'copilot.read')
        self.client.force_login(other)
        resp = self.post_json('copilot:feedback',
                              {'message_id': self.answer.pk, 'vote': 1})
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(CopilotFeedback.objects.exists())

    def test_feedback_state_returned_in_history(self):
        self.post_json('copilot:feedback', {'message_id': self.answer.pk, 'vote': -1})
        data = self.client.get(
            reverse('copilot:history', args=[self.conversation.pk])).json()['data']
        assistant = [m for m in data['messages'] if m['role'] == 'assistant'][0]
        self.assertEqual(assistant['vote'], -1)

    def test_promote_creates_inactive_draft(self):
        CopilotFeedback.objects.create(message=self.answer, user=self.user, vote=-1)
        out = StringIO()
        call_command('copilot_promote', stdout=out)
        case = CopilotEvalCase.objects.get()
        self.assertFalse(case.active)
        self.assertEqual(case.mode, CopilotEvalCase.Mode.MODEL)
        self.assertEqual(case.question, '华建建材的客诉有哪些')
        self.assertEqual(case.module_scope, 'customer_complaint')
        self.assertIsNotNone(case.source_feedback)
        self.assertIn('已生成评估用例草稿 1 条', out.getvalue())

    def test_promote_dry_run_writes_nothing(self):
        CopilotFeedback.objects.create(message=self.answer, user=self.user, vote=-1)
        out = StringIO()
        call_command('copilot_promote', '--dry-run', stdout=out)
        self.assertFalse(CopilotEvalCase.objects.exists())
        self.assertIn('[dry-run]', out.getvalue())

    def test_promote_skips_already_converted(self):
        feedback = CopilotFeedback.objects.create(
            message=self.answer, user=self.user, vote=-1)
        CopilotEvalCase.objects.create(question='已有的', source_feedback=feedback)
        call_command('copilot_promote', stdout=StringIO())
        self.assertEqual(CopilotEvalCase.objects.count(), 1)


class ConversationTests(BaseCase):
    def _second(self, title, uid):
        conv = CopilotConversation.objects.create(user=self.user, title=title)
        CopilotMessage.objects.create(conversation=conv, role='user', content=uid)
        CopilotConversation.objects.filter(pk=conv.pk).update(last_active_at=timezone.now())
        return conv

    def test_list_returns_recent_first(self):
        self._second('较新的会话', '新问题')
        resp = self.client.get(reverse('copilot:conversations'))
        self.assertEqual(resp.status_code, 200)
        items = resp.json()['data']['items']
        self.assertEqual(items[0]['title'], '较新的会话')
        self.assertEqual(items[0]['message_count'], 1)
        self.assertEqual(len(items), 2)

    def test_rename_and_blank_title(self):
        url = reverse('copilot:rename_conversation', args=[self.conversation.pk])
        ok = self.client.post(url, data=json.dumps({'title': '  月度 客诉  '}),
                              content_type='application/json')
        self.assertEqual(ok.status_code, 200)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.title, '月度 客诉')
        blank = self.client.post(url, data=json.dumps({'title': '   '}),
                                 content_type='application/json')
        self.assertEqual(blank.status_code, 400)

    def test_delete_archives_and_latest_falls_back(self):
        older = self._second('更早的会话', '更早的问题')
        CopilotConversation.objects.filter(pk=older.pk).update(
            last_active_at=timezone.now() - timedelta(hours=2))
        url = reverse('copilot:delete_conversation', args=[self.conversation.pk])
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 200)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.status, CopilotConversation.Status.ARCHIVED)
        self.assertIsNotNone(self.conversation.archived_at)

        latest = self.client.get(reverse('copilot:latest_conversation')).json()['data']
        self.assertEqual(latest['conversation_id'], older.pk)

    def test_deleted_conversation_history_404(self):
        url = reverse('copilot:delete_conversation', args=[self.conversation.pk])
        self.client.post(url)
        resp = self.client.get(reverse('copilot:history', args=[self.conversation.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_delete_cancels_running_run(self):
        run = CopilotRun.objects.create(
            conversation=self.conversation, user=self.user, question='q',
            status=CopilotRun.Status.RUNNING)
        self.client.post(reverse('copilot:delete_conversation',
                                 args=[self.conversation.pk]))
        run.refresh_from_db()
        self.assertEqual(run.status, CopilotRun.Status.CANCELLED)


class PruneTests(BaseCase):
    def _make_egress(self, days_ago):
        log = CopilotEgressLog.objects.create(provider='minimax', model='m')
        CopilotEgressLog.objects.filter(pk=log.pk).update(
            created_at=timezone.now() - timedelta(days=days_ago))
        return log

    def test_default_is_dry_run(self):
        CopilotConversation.objects.filter(pk=self.conversation.pk).update(
            last_active_at=timezone.now() - timedelta(days=200))
        out = StringIO()
        call_command('copilot_prune', stdout=out)
        self.assertIn('dry-run', out.getvalue())
        self.assertTrue(CopilotConversation.objects.filter(pk=self.conversation.pk).exists())

    def test_confirm_deletes_expired_sessions_only(self):
        fresh = CopilotConversation.objects.create(user=self.user, title='新会话')
        CopilotConversation.objects.filter(pk=self.conversation.pk).update(
            last_active_at=timezone.now() - timedelta(days=200))
        call_command('copilot_prune', '--confirm', stdout=StringIO())
        self.assertFalse(CopilotConversation.objects.filter(pk=self.conversation.pk).exists())
        self.assertTrue(CopilotConversation.objects.filter(pk=fresh.pk).exists())
        # 消息随会话级联删除
        self.assertFalse(CopilotMessage.objects.filter(
            conversation_id=self.conversation.pk).exists())

    def test_egress_kept_longer_than_sessions(self):
        old_log = self._make_egress(100)      # 100 天：会话早过期，日志还在保留期
        expired_log = self._make_egress(200)
        call_command('copilot_prune', '--confirm', stdout=StringIO())
        self.assertTrue(CopilotEgressLog.objects.filter(pk=old_log.pk).exists())
        self.assertFalse(CopilotEgressLog.objects.filter(pk=expired_log.pk).exists())

    def test_archived_grace_period(self):
        self.conversation.status = CopilotConversation.Status.ARCHIVED
        self.conversation.archived_at = timezone.now() - timedelta(days=3)
        self.conversation.save()
        call_command('copilot_prune', '--confirm', stdout=StringIO())
        self.assertTrue(CopilotConversation.objects.filter(pk=self.conversation.pk).exists())

    def test_stranded_run_marked_interrupted(self):
        """会话还新、但 run 是很久以前卡住的：只把 run 收尾，不删会话。"""
        run = CopilotRun.objects.create(
            conversation=self.conversation, user=self.user, question='q',
            status=CopilotRun.Status.RUNNING)
        CopilotRun.objects.filter(pk=run.pk).update(
            created_at=timezone.now() - timedelta(days=200))
        call_command('copilot_prune', '--confirm', stdout=StringIO())
        run.refresh_from_db()
        self.assertEqual(run.status, CopilotRun.Status.INTERRUPTED)


@override_settings(CACHES=LOCMEM)
class PermissionRecheckTests(BaseCase):
    def _user_with(self, *codes):
        user = User.objects.create_user('perm_user', password='p')
        _grant(user, 'copilot.read', *codes)
        return user

    def test_restricted_answer_hidden_and_refs_dropped(self):
        user = self._user_with('customer_complaint.read')
        conv = CopilotConversation.objects.create(user=user, title='t')
        CopilotMessage.objects.create(conversation=conv, role='user', content='客诉')
        msg = CopilotMessage.objects.create(
            conversation=conv, role='assistant', content='华建建材 1 条客诉',
            refs=[{'module': 'customer_complaint', 'label': '华建建材', 'id': 1}])
        self.client.force_login(user)

        shown = self.client.get(reverse('copilot:history', args=[conv.pk])).json()['data']
        self.assertFalse(shown['messages'][1]['restricted'])
        self.assertTrue(shown['messages'][1]['refs'])

        # 撤权
        role = Role.objects.get(code='role_perm_user')
        role.permissions.remove(Permission.objects.get(code='customer_complaint.read'))
        hidden = self.client.get(reverse('copilot:history', args=[conv.pk])).json()['data']
        answer = hidden['messages'][1]
        self.assertTrue(answer['restricted'])
        self.assertIn('无权访问', answer['content'])
        self.assertNotIn('华建建材', answer['content_html'])
        self.assertEqual(answer['refs'], [])
        self.assertEqual(answer['restricted_modules'], ['customer_complaint'])

    def test_permission_restored_shows_again(self):
        user = self._user_with('customer_complaint.read')
        conv = CopilotConversation.objects.create(user=user, title='t')
        CopilotMessage.objects.create(
            conversation=conv, role='assistant', content='华建建材 1 条',
            refs=[{'module': 'customer_complaint', 'label': '华建建材'}])
        self.client.force_login(user)
        role = Role.objects.get(code='role_perm_user')
        perm = Permission.objects.get(code='customer_complaint.read')
        role.permissions.remove(perm)
        role.permissions.add(perm)
        data = self.client.get(reverse('copilot:history', args=[conv.pk])).json()['data']
        self.assertFalse(data['messages'][0]['restricted'])
        self.assertEqual(len(data['messages'][0]['refs']), 1)


class ModelEvalTests(TestCase):
    """只测判定逻辑与报告渲染，不真实调用模型（那属于 --with-model 手工基线）。"""

    def _run(self, status=CopilotRun.Status.SUCCEEDED, answer='', steps=None, refs=None):
        run = CopilotRun(status=status, answer=answer,
                         steps=steps or [], refs=refs or [])
        return run

    def test_verdict_pass(self):
        from copilot.model_cases import ModelCase
        from copilot.model_eval import _verdict
        case = ModelCase('客诉最多的客户等级', ('query_records',), ('客户',),
                         scope='customer_complaint')
        run = self._run(answer='客户等级是 A 类。',
                        steps=[{'tool': 'query_records', 'module': 'customer_complaint'}],
                        refs=[{'module': 'customer_complaint', 'label': 'x'}])
        checks = _verdict(case, run, run.answer)
        self.assertTrue(all(checks.values()), checks)

    def test_verdict_fails_on_wrong_tool_and_excuse(self):
        from copilot.model_cases import ModelCase
        from copilot.model_eval import _verdict
        case = ModelCase('客诉最多的客户等级', ('query_records',), ('客户',),
                         scope='customer_complaint')
        run = self._run(answer='抱歉，我无法回答这个问题。',
                        steps=[{'tool': 'search_knowledge'}])
        checks = _verdict(case, run, run.answer)
        self.assertFalse(checks['tool'])
        self.assertFalse(checks['forbidden'])
        self.assertFalse(checks['scope'])

    def test_report_render(self):
        from copilot.model_eval import Attempt, ModelReport, ModelResult, render_markdown
        report = ModelReport(provider='ollama', model='qwen2.5', rounds=2)
        report.results.append(ModelResult(
            question='q1', ok=True,
            attempts=[Attempt(ok=True), Attempt(ok=True)]))
        report.results.append(ModelResult(
            question='q2', ok=False, failure='工具调错（识别问题）',
            attempts=[Attempt(ok=False, tools=['global_search'], answer=''),
                      Attempt(ok=False, answer='')]))
        text = render_markdown(report)
        self.assertIn('通过率：**50.0%**', text)
        self.assertIn('工具调错', text)
        self.assertIn('q2', text)

    def test_seed_model_cases(self):
        User.objects.create_superuser('eval_admin', 'e@x.com', 'p')
        out = StringIO()
        call_command('copilot_eval', '--seed-model-cases', '--no-local',
                     '--user', 'eval_admin', stdout=out)
        self.assertGreaterEqual(CopilotEvalCase.objects.count(), 55)
        self.assertTrue(all(c.mode == 'model' for c in CopilotEvalCase.objects.all()))
        # 再跑一次不重复写入
        call_command('copilot_eval', '--seed-model-cases', '--no-local',
                     '--user', 'eval_admin', stdout=StringIO())
        self.assertEqual(CopilotEvalCase.objects.count(), len(
            {c.question for c in CopilotEvalCase.objects.all()}))
