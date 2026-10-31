"""接口与执行流程的端到端测试（模型用假实现，不联网）。"""

import json
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ai_api.models import AiProviderConfig
from copilot.models import (CopilotConversation, CopilotEgressLog, CopilotMessage,
                            CopilotPolicy, CopilotRun, CopilotToolCall)
from copilot.providers.base import ChatResult, ProviderError
from records.models import CustomerComplaint, Regulation

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
User = get_user_model()


@override_settings(CACHES=LOCMEM, COPILOT_INLINE_EXECUTION=True)
class ApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_superuser('api_admin', 'a@x.com', 'p')
        self.client.force_login(self.user)
        self.config = AiProviderConfig.objects.create(
            purpose=AiProviderConfig.Purpose.CHAT,
            provider=AiProviderConfig.Provider.OLLAMA,
            model='test-model', is_enabled=True, is_local=True, max_tokens=256,
        )
        CopilotPolicy.objects.all().delete()
        CopilotPolicy.objects.create(rate_limit_per_10min=100, rate_limit_per_day=1000)
        CustomerComplaint.objects.create(
            customer_name='华建建材', complaint_date=date(2026, 9, 10),
            complaint_status='处理中')

    def _post(self, name, payload, **kwargs):
        return self.client.post(
            reverse(name), data=json.dumps(payload),
            content_type='application/json', **kwargs)

    def test_bootstrap(self):
        resp = self.client.get(reverse('copilot:bootstrap'))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertTrue(data['configured'])
        self.assertTrue(data['is_local'])
        self.assertIn('customer_complaint', [m['key'] for m in data['modules']])
        self.assertTrue(data['examples'])

    def test_ask_success_with_tool_and_refs(self):
        sequence = [
            ChatResult(content='{"tool":"query_records",'
                               '"args":{"module":"customer_complaint","limit":5}}',
                       prompt_tokens=10, completion_tokens=5),
            ChatResult(content='上个月华建建材有 1 条客诉。',
                       prompt_tokens=12, completion_tokens=8),
        ]
        with patch('copilot.providers.router.select',
                   return_value=(self.config, None)), \
             patch('copilot.providers.router.chat_with',
                   side_effect=lambda *a, **k: sequence.pop(0)):
            resp = self._post('copilot:ask', {'question': '上个月客诉有哪些'})

        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertTrue(data['finished'])
        self.assertEqual(data['status'], 'succeeded')
        self.assertIn('华建', data['answer'])
        self.assertTrue(data['refs'])
        self.assertEqual(data['refs'][0]['module'], 'customer_complaint')
        self.assertEqual(data['steps'][0]['tool'], 'query_records')
        self.assertTrue(CopilotToolCall.objects.filter(permitted=True).exists())
        self.assertTrue(CopilotMessage.objects.filter(role='assistant').exists())

    def test_ask_rejects_short_question(self):
        resp = self._post('copilot:ask', {'question': 'a'})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['error'], 'EMPTY_QUESTION')

    def test_ask_requires_permission(self):
        outsider = User.objects.create_user('api_outsider', password='p')
        self.client.force_login(outsider)
        resp = self._post('copilot:ask', {'question': '上个月客诉有哪些'})
        self.assertEqual(resp.status_code, 403)

    def test_ask_degrades_when_model_unavailable(self):
        with patch('copilot.providers.router.select',
                   side_effect=ProviderError('网络不通', 'NETWORK')):
            resp = self._post('copilot:ask', {'question': '华建建材的客诉'})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertEqual(data['status'], 'succeeded')
        self.assertEqual(data['error_code'], 'DEGRADED')
        self.assertIn('AI 暂时不可用', data['answer'])
        # 降级必须真去检索：步骤要有 keyword_fallback，且拿到可点击的候选
        self.assertEqual(data['steps'][0]['tool'], 'keyword_fallback')
        self.assertTrue(data['steps'][0]['ok'])
        self.assertTrue(data['refs'])
        self.assertEqual(data['refs'][0]['module'], 'customer_complaint')

    def test_rate_limit(self):
        CopilotPolicy.objects.all().update(rate_limit_per_10min=1)
        with patch('copilot.providers.router.select',
                   side_effect=ProviderError('x', 'NETWORK')):
            first = self._post('copilot:ask', {'question': '第一个问题'})
            second = self._post('copilot:ask', {'question': '第二个问题'})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()['error'], 'RATE_LIMITED')

    def test_sanitize_fake_links_in_answer(self):
        sequence = [
            ChatResult(content='{"tool":"query_records",'
                               '"args":{"module":"customer_complaint","limit":5}}'),
            ChatResult(content='见 [记录](/records/customer_complaint/detail/999/)'),
        ]
        with patch('copilot.providers.router.select',
                   return_value=(self.config, None)), \
             patch('copilot.providers.router.chat_with',
                   side_effect=lambda *a, **k: sequence.pop(0)):
            resp = self._post('copilot:ask', {'question': '客诉详情'})
        answer = resp.json()['data']['answer']
        self.assertNotIn('/records/customer_complaint/detail/999/', answer)
        self.assertIn('见下方引用', answer)

    def test_run_detail_and_isolation(self):
        conversation = CopilotConversation.objects.create(user=self.user, title='t')
        run = CopilotRun.objects.create(
            conversation=conversation, user=self.user, question='q',
            status=CopilotRun.Status.SUCCEEDED, answer='a')
        resp = self.client.get(reverse('copilot:run_detail', args=[run.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['answer'], 'a')

        other = User.objects.create_user('api_other', password='p')
        from accounts.models import Permission, Role
        role = Role.objects.create(code='iso_role', name='隔离测试')
        perm = Permission.objects.filter(code='copilot.read').first()
        if perm:
            role.permissions.add(perm)
        role.users.add(other)
        self.client.force_login(other)
        resp = self.client.get(reverse('copilot:run_detail', args=[run.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_cancel_pending_run(self):
        conversation = CopilotConversation.objects.create(user=self.user, title='t')
        run = CopilotRun.objects.create(
            conversation=conversation, user=self.user, question='q',
            status=CopilotRun.Status.PENDING)
        resp = self.client.post(reverse('copilot:cancel', args=[run.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['status'], 'cancelled')

    def test_egress_logged_for_cloud_model(self):
        cloud = AiProviderConfig.objects.create(
            purpose=AiProviderConfig.Purpose.CHAT,
            provider=AiProviderConfig.Provider.MINIMAX,
            model='cloud-model', is_enabled=True, is_local=False,
        )
        Regulation.objects.create(title='考勤制度', doc_no='ZD-001',
                                  issue_date=date(2026, 1, 1))
        sequence = [
            ChatResult(content='{"tool":"query_records","args":{"module":"regulation"}}'),
            ChatResult(content='共 1 条制度。'),
        ]
        with patch('copilot.providers.router.select',
                   return_value=(cloud, None)), \
             patch('copilot.providers.router.chat_with',
                   side_effect=lambda *a, **k: sequence.pop(0)):
            resp = self._post('copilot:ask', {'question': '有哪些制度'})

        self.assertEqual(resp.status_code, 200)
        log = CopilotEgressLog.objects.first()
        self.assertIsNotNone(log)
        self.assertFalse(log.is_local)
        self.assertIn('regulation', log.module_keys)

    def test_unsupported_tool_gets_fixed_reply(self):
        sequence = [
            ChatResult(content='{"tool":"delete_records","args":'
                               '{"module":"customer_complaint"}}'),
            ChatResult(content='已删除'),
        ]
        with patch('copilot.providers.router.select',
                   return_value=(self.config, None)), \
             patch('copilot.providers.router.chat_with',
                   side_effect=lambda *a, **k: sequence.pop(0)):
            resp = self._post('copilot:ask', {'question': '把所有客诉删掉'})
        data = resp.json()['data']
        self.assertIn('不能执行该操作', data['answer'])
        self.assertNotIn('"tool"', data['answer'])
        self.assertEqual(data['steps'][0]['error'], 'UNSUPPORTED_TOOL')

    def test_history_endpoint(self):
        conversation = CopilotConversation.objects.create(user=self.user, title='t')
        CopilotMessage.objects.create(conversation=conversation, role='user',
                                      content='问题')
        CopilotMessage.objects.create(conversation=conversation, role='assistant',
                                      content='回答')
        resp = self.client.get(reverse('copilot:history', args=[conversation.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()['data']['messages']), 2)

    def test_latest_conversation_restores_across_pages(self):
        """换页面后前端能接上最近一次会话（按 last_active_at 取，不按创建时间）。"""
        old = CopilotConversation.objects.create(user=self.user, title='旧会话')
        CopilotMessage.objects.create(conversation=old, role='user', content='旧问题')
        new = CopilotConversation.objects.create(user=self.user, title='新会话')
        CopilotMessage.objects.create(conversation=new, role='user', content='新问题')
        CopilotConversation.objects.filter(pk=new.pk).update(last_active_at=timezone.now())

        resp = self.client.get(reverse('copilot:latest_conversation'))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()['data']
        self.assertEqual(data['conversation_id'], new.pk)
        self.assertEqual([m['content'] for m in data['messages']], ['新问题'])

    def test_latest_conversation_is_user_scoped(self):
        other = User.objects.create_user('latest_other', password='p')
        theirs = CopilotConversation.objects.create(user=other, title='别人的')
        CopilotMessage.objects.create(conversation=theirs, role='user', content='别人的问题')

        resp = self.client.get(reverse('copilot:latest_conversation'))
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()['data']['conversation_id'])
        self.assertEqual(resp.json()['data']['messages'], [])

    def test_ask_reuses_given_conversation(self):
        """前端带上 conversation_id 时应续写同一会话，而不是新建。"""
        conversation = CopilotConversation.objects.create(user=self.user, title='t')
        with patch('copilot.providers.router.select',
                   side_effect=ProviderError('网络不通', 'NETWORK')):
            resp = self._post('copilot:ask', {'question': '华建建材的客诉',
                                              'conversation_id': conversation.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['conversation_id'], conversation.pk)
        self.assertEqual(CopilotConversation.objects.filter(user=self.user).count(), 1)

    def test_feedback(self):
        conversation = CopilotConversation.objects.create(user=self.user, title='t')
        message = CopilotMessage.objects.create(
            conversation=conversation, role='assistant', content='回答')
        resp = self._post('copilot:feedback',
                          {'message_id': message.pk, 'vote': -1, 'comment': '不准'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['vote'], -1)


@override_settings(CACHES=LOCMEM)
class ReapTests(TestCase):
    def test_stale_run_marked_timeout(self):
        from copilot import tasks
        user = User.objects.create_superuser('reap_admin', 'r@x.com', 'p')
        conversation = CopilotConversation.objects.create(user=user)
        run = CopilotRun.objects.create(
            conversation=conversation, user=user, question='q',
            status=CopilotRun.Status.RUNNING,
            heartbeat_at=timezone.now() - timedelta(minutes=10))
        count = tasks.reap_stale_runs()
        self.assertEqual(count, 1)
        run.refresh_from_db()
        self.assertEqual(run.status, CopilotRun.Status.TIMEOUT)
