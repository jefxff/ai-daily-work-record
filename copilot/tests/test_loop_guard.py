"""闲聊快路径与工具循环熔断。

锁住用户反馈的两个现象：
1. 输入「你好」，模型反复调 list_modules，跑满轮次后回一句「超出单次上限」；
2. 小模型反复用同样的错误参数调工具，直到触顶也没有答案。

期望行为：闲聊本地直接答（不调模型）；重复调用先跳过再熔断；触顶后用一次
「禁用工具」的收尾调用把已有结果变成答案，而不是把上限提示丢给用户。
"""

import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from ai_api.models import AiProviderConfig
from copilot import registry
from copilot.engine import planner
from copilot.engine import prompt as prompt_engine
from copilot.engine.executor import (_call_signature, _mark_call,
                                     _too_many_failures)
from copilot.models import CopilotPolicy
from copilot.providers.base import ChatResult
from copilot.registry import _args_hint, get
from records.models import CustomerComplaint

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
User = get_user_model()


class SmalltalkTests(TestCase):
    def test_greetings_are_detected(self):
        for text in ['你好', '您好！', '你好呀', 'hello', 'HELLO', '在吗',
                     '早上好', '谢谢', '多谢！', '你是谁', '你能做什么']:
            with self.subTest(text=text):
                self.assertTrue(planner.is_smalltalk(text))

    def test_real_questions_are_not_smalltalk(self):
        for text in ['华建建材的客诉有哪些', '最近发布了哪些制度',
                     '你好，查一下华建建材的客诉', '帮我统计本月客诉']:
            with self.subTest(text=text):
                self.assertFalse(planner.is_smalltalk(text))

    def test_quick_reply_lists_modules(self):
        admin = User.objects.create_superuser('st_admin', 'st@x.com', 'p')
        modules = planner.registry.available_modules(admin, True, CopilotPolicy.current())
        reply = planner.quick_reply('你好', modules)
        self.assertIn('站内 AI 助手', reply)
        self.assertIn(modules[0].label, reply)

    def test_thanks_and_bye_have_short_replies(self):
        self.assertIn('不客气', planner.quick_reply('谢谢', []))
        self.assertIn('再叫我', planner.quick_reply('再见', []))


class SignatureTests(TestCase):
    def test_signature_ignores_key_order(self):
        left = _call_signature('query_records', {'module': 'a', 'limit': 5})
        right = _call_signature('query_records', {'limit': 5, 'module': 'a'})
        self.assertEqual(left, right)

    def test_mark_call_counts_from_zero(self):
        seen = {}
        self.assertEqual(_mark_call(seen, 't', {'a': 1}), 0)
        self.assertEqual(_mark_call(seen, 't', {'a': 1}), 1)
        self.assertEqual(_mark_call(seen, 't', {'a': 2}), 0)

    def test_failure_aborts_after_two_same_errors(self):
        failures = {}
        bad = SimpleNamespace(ok=False, error='BAD_ARGS')
        self.assertFalse(_too_many_failures(failures, 'query_records', bad))
        self.assertTrue(_too_many_failures(failures, 'query_records', bad))
        # 成功一次不计数
        self.assertFalse(_too_many_failures(
            failures, 'query_records', SimpleNamespace(ok=True, error='')))


class ArgsHintTests(TestCase):
    def test_bad_args_hint_lists_allowed_values(self):
        """参数报错要带上可选值，模型才有机会在下一轮改对。"""
        admin = User.objects.create_superuser('hint_admin', 'hint@x.com', 'p')
        ctx = SimpleNamespace(user=admin, is_local=True,
                              policy=CopilotPolicy.current())
        hint = _args_hint(get('query_records'), ctx)
        self.assertIn('可用参数', hint)
        self.assertIn('customer_complaint', hint)


class PromptTests(TestCase):
    def test_prompt_mentions_blocked_modules_and_today(self):
        """云端模式下要明说"这些模块不能查"，否则模型会把策略限制答成"没有记录"。"""
        admin = User.objects.create_superuser('pr_admin', 'pr@x.com', 'p')
        policy = CopilotPolicy.current()
        local = registry.available_modules(admin, True, policy)
        cloud = registry.available_modules(admin, False, policy)
        keys = {m.key for m in cloud}
        blocked = [m for m in local if m.key not in keys]
        self.assertTrue(blocked)

        text = prompt_engine.build_system_prompt(cloud, [], blocked=blocked,
                                                 today='2026-10-31')
        self.assertIn('数据外发限制', text)
        self.assertIn(blocked[0].label, text)
        self.assertIn('2026-10-31', text)
        self.assertIn('不要猜年份', text)


@override_settings(CACHES=LOCMEM, COPILOT_INLINE_EXECUTION=True)
class ExecutorLoopTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_superuser('loop_admin', 'loop@x.com', 'p')
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

    def _ask(self, question):
        return self.client.post(
            reverse('copilot:ask'), data=json.dumps({'question': question}),
            content_type='application/json').json()['data']

    def test_greeting_is_answered_without_model(self):
        """问候不该进工具循环：连模型都不调，直接给答案。"""
        with patch('copilot.providers.router.chat_with',
                   side_effect=AssertionError('问候不应调用模型')):
            data = self._ask('你好')
        self.assertEqual(data['status'], 'succeeded')
        self.assertEqual(data['steps'], [])
        self.assertIn('AI 助手', data['answer'])

    def test_repeated_tool_call_falls_through_to_forced_answer(self):
        same = '{"tool":"list_modules","args":{}}'
        sequence = [
            ChatResult(content=same, prompt_tokens=8, completion_tokens=4),
            ChatResult(content=same, prompt_tokens=8, completion_tokens=4),
            ChatResult(content=same, prompt_tokens=8, completion_tokens=4),
            ChatResult(content='你可以问我客诉、保险或制度相关的数据。',
                       prompt_tokens=10, completion_tokens=6),
        ]
        calls = []

        def fake_chat(*args, **kwargs):
            calls.append(kwargs.get('tools'))
            return sequence.pop(0)

        with patch('copilot.providers.router.select',
                   return_value=(self.config, None)), \
             patch('copilot.providers.router.chat_with', side_effect=fake_chat):
            data = self._ask('站内有哪些数据')

        self.assertEqual(data['status'], 'succeeded')
        self.assertIn('可以问我', data['answer'])
        # 第 2 次重复被跳过、第 3 次触发熔断；连续重复合并成一条并累计次数
        errors = [s.get('error') for s in data['steps'] if not s['ok']]
        self.assertEqual(errors, ['重复调用已跳过 2 次'])
        # 最后一次收尾调用必须禁掉工具
        self.assertIsNone(calls[-1])
        self.assertEqual(data['error_code'], '')

    def test_forced_answer_falls_back_to_refs_digest(self):
        """收尾调用也不给文字时，把已查到的记录直接列出来。"""
        sequence = [
            ChatResult(content='{"tool":"query_records",'
                               '"args":{"module":"customer_complaint","limit":5}}'),
            ChatResult(content='{"tool":"query_records",'
                               '"args":{"module":"customer_complaint","limit":5}}'),
            ChatResult(content='{"tool":"query_records",'
                               '"args":{"module":"customer_complaint","limit":5}}'),
            ChatResult(content='{"tool":"query_records","args":{}}'),
        ]
        with patch('copilot.providers.router.select',
                   return_value=(self.config, None)), \
             patch('copilot.providers.router.chat_with',
                   side_effect=lambda *a, **k: sequence.pop(0)):
            data = self._ask('客诉的处理进展怎么样')

        self.assertEqual(data['status'], 'succeeded')
        self.assertIn('已查到以下记录', data['answer'])
        self.assertTrue(data['refs'])

    def test_force_answer_not_triggered_on_normal_answer(self):
        sequence = [ChatResult(content='客诉目前都在处理中。')]
        with patch('copilot.providers.router.select',
                   return_value=(self.config, None)), \
             patch('copilot.providers.router.chat_with',
                   side_effect=lambda *a, **k: sequence.pop(0)):
            data = self._ask('客诉的处理进展怎么样')
        self.assertEqual(data['answer'], '客诉目前都在处理中。')
        self.assertEqual(data['steps'], [])

    def test_bootstrap_reports_blocked_modules_in_cloud_mode(self):
        AiProviderConfig.objects.filter(pk=self.config.pk).update(is_local=False)
        data = self.client.get(reverse('copilot:bootstrap')).json()['data']
        self.assertFalse(data['is_local'])
        self.assertIn('客户客诉', data['blocked'])

    def test_cloud_mode_refuses_blocked_module_deterministically(self):
        """问的就全是禁止外发的模块时，必须直说"不能发云端"，不能说"没有记录"。"""
        AiProviderConfig.objects.filter(pk=self.config.pk).update(is_local=False)
        with patch('copilot.providers.router.chat_with',
                   side_effect=AssertionError('被拦的模块不该调模型')):
            data = self._ask('华建建材有哪些客诉')
        self.assertEqual(data['error_code'], 'EGRESS_BLOCKED')
        self.assertIn('云端模型', data['answer'])
        self.assertIn('不允许发送到外部模型', data['answer'])
        self.assertEqual(data['steps'][0]['tool'], 'egress_blocked')
