"""答案质量：工具名纠错与"记录 JSON 当答案"的改写。

锁住 2026-10-31 用户截图反馈的两个现象（本地 7B 模型下）：

1. 问「今日铜价是多少？」，助手回「抱歉，我不能执行该操作。系统只开放查询类能力，
   删除或修改请到对应页面手动完成。」——模型输出的工具名是模块 key ``metal_price``，
   注册表没有同名工具，于是被判成"不支持的操作"。用户看到的是完全对不上问题的回答。
2. 问「月结 60 天的客户有哪些」，助手把工具返回的一条记录原样贴出来：
   ``{"客户名称":"奥盛精密机械有限公司","提出日期":"2026-09-05",…}``。

期望：模块 key 当工具名 → 自动改写成 query_records(module=...)，正常查；
真正的未知工具 → 先给模型一次纠正机会，再不行才回"找不到入口"；
写操作意图 → 保持原来的拒绝文案；记录 JSON → 转成可读文本。
"""

import json
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from ai_api.models import AiProviderConfig
from copilot import registry
from copilot.engine import parser
from copilot.engine import prompt as prompt_engine
from copilot.models import CopilotPolicy
from copilot.providers.base import ChatResult
from records.models import CustomerComplaint, MetalPrice

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
User = get_user_model()


class ToolNameResolutionTests(TestCase):
    def test_module_key_maps_to_query_records(self):
        self.assertEqual(registry.resolve_tool_name('metal_price'),
                         ('query_records', 'metal_price'))

    def test_real_tool_passes_through(self):
        self.assertEqual(registry.resolve_tool_name('query_records'),
                         ('query_records', ''))

    def test_unknown_name_is_untouched(self):
        self.assertEqual(registry.resolve_tool_name('cu_price'), ('cu_price', ''))

    def test_write_intent_detected(self):
        self.assertTrue(registry.is_write_intent('delete_complaint'))
        self.assertTrue(registry.is_write_intent('更新台账'))
        self.assertFalse(registry.is_write_intent('metal_price'))


class RecordJsonDetectionTests(TestCase):
    def test_record_object_is_detected(self):
        text = ('{"客户名称":"奥盛精密机械有限公司","提出日期":"2026-09-05",'
                '"业务员":"马晨刚","结果":"有条件通过","id":11}')
        obj = parser.as_record_object(text)
        self.assertIsNotNone(obj)
        self.assertEqual(obj['客户名称'], '奥盛精密机械有限公司')

    def test_single_field_record_is_detected(self):
        """真实模型答铜价时给的就是 {"铜":"45.69 美元/吨"}——一个字段也算记录。"""
        self.assertIsNotNone(parser.as_record_object('{"铜":"45.69 美元/吨"}'))

    def test_normal_sentence_is_not_a_record(self):
        self.assertIsNone(parser.as_record_object(
            '查到 4 条客户账期，最近一条是奥盛精密机械有限公司。'))

    def test_protocol_json_is_not_a_record(self):
        self.assertIsNone(parser.as_record_object('{"answer":"好的","tool":"x"}'))

    def test_chinese_protocol_keys_are_recognised(self):
        """本地中文模型会输出 {"工具":"get_record","参数":{...}}。"""
        for text in ('{"工具":"get_record","args":{}}',
                     '{"动作":"query_records","参数":{"module":"metal_price"}}'):
            with self.subTest(text=text):
                self.assertTrue(parser.has_protocol_keys(parser.as_json_object(text)))
                self.assertIsNone(parser.as_record_object(text))


class ChineseToolKeyTests(TestCase):
    def test_chinese_tool_key_is_parsed_as_tool_call(self):
        decision = parser.parse_decision(
            '{"工具":"query_records","参数":{"module":"metal_price","limit":3}}',
            {'query_records'})
        self.assertEqual(decision['type'], 'tool')
        self.assertEqual(decision['name'], 'query_records')
        self.assertEqual(decision['args']['module'], 'metal_price')

    def test_chinese_tool_key_with_args_key(self):
        decision = parser.parse_decision(
            '{"工具":"get_record","args":{"module":"metal_price","record_id":2}}',
            {'get_record'})
        self.assertEqual(decision['type'], 'tool')
        self.assertEqual(decision['args']['record_id'], 2)


class PromptGuidanceTests(TestCase):
    def test_prompt_says_module_key_is_not_tool(self):
        admin = User.objects.create_superuser('p_guide', 'g@x.com', 'p')
        policy = CopilotPolicy.current()
        modules = registry.available_modules(admin, True, policy)
        specs = registry.available_specs(admin, True, policy)
        text = prompt_engine.build_system_prompt(modules, specs, today='2026-10-31')
        self.assertIn('不是工具名', text)
        self.assertIn('query_records', text)
        self.assertIn('不要把 JSON 记录原样抄给用户', text)


@override_settings(CACHES=LOCMEM, COPILOT_INLINE_EXECUTION=True)
class AnswerQualityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_superuser('aq_admin', 'aq@x.com', 'p')
        self.client.force_login(self.user)
        self.config = AiProviderConfig.objects.create(
            purpose=AiProviderConfig.Purpose.CHAT,
            provider=AiProviderConfig.Provider.OLLAMA,
            model='test-model', is_enabled=True, is_local=True, max_tokens=256,
        )
        CopilotPolicy.objects.all().delete()
        CopilotPolicy.objects.create(rate_limit_per_10min=100, rate_limit_per_day=1000)
        CustomerComplaint.objects.create(
            customer_name='奥盛精密机械有限公司', complaint_date=date(2026, 9, 5),
            complaint_status='处理中')
        CustomerComplaint.objects.create(
            customer_name='西部精密机械有限公司', complaint_date=date(2026, 9, 12),
            complaint_status='处理中')
        MetalPrice.objects.create(metal_name='铜', price=68000, unit='元/吨')

    def _ask(self, question):
        return self.client.post(
            reverse('copilot:ask'), data=json.dumps({'question': question}),
            content_type='application/json').json()['data']

    def _run(self, question, sequence):
        with patch('copilot.providers.router.select',
                   return_value=(self.config, None)), \
             patch('copilot.providers.router.chat_with',
                   side_effect=lambda *a, **k: sequence.pop(0)):
            return self._ask(question)

    def test_module_key_as_tool_name_is_rewritten(self):
        """{"tool":"metal_price"} 应当被当成"查金属价格模块"。"""
        sequence = [
            ChatResult(content='{"tool":"metal_price","args":{"limit":3}}'),
            ChatResult(content='最新铜价 68000 元/吨。'),
        ]
        data = self._run('今日铜价是多少', sequence)
        self.assertEqual(data['status'], 'succeeded')
        self.assertEqual(data['answer'], '最新铜价 68000 元/吨。')
        step = data['steps'][0]
        self.assertEqual(step['tool'], 'query_records')
        self.assertEqual(step['module'], 'metal_price')
        self.assertTrue(step['ok'])
        self.assertNotIn('UNSUPPORTED_TOOL', [s.get('error') for s in data['steps']])

    def test_unknown_tool_retries_once_before_giving_up(self):
        """完全不认识的工具名：第一次回提示让模型改，第二次才终止。"""
        unknown = '{"tool":"cu_price_today","args":{}}'
        sequence = [
            ChatResult(content=unknown),
            ChatResult(content=unknown),
        ]
        data = self._run('今日铜价是多少', sequence)
        self.assertEqual(data['error_code'], 'UNSUPPORTED_TOOL')
        self.assertIn('没能找到', data['answer'])
        self.assertNotIn('删除或修改', data['answer'])
        self.assertEqual(data['steps'][0]['error'], 'UNSUPPORTED_TOOL')

    def test_unknown_tool_can_recover_on_second_round(self):
        sequence = [
            ChatResult(content='{"tool":"cu_price_today","args":{}}'),
            ChatResult(content='{"tool":"query_records",'
                               '"args":{"module":"metal_price","limit":3}}'),
            ChatResult(content='铜价 68000 元/吨。'),
        ]
        data = self._run('今日铜价是多少', sequence)
        self.assertEqual(data['answer'], '铜价 68000 元/吨。')
        self.assertTrue(data['refs'])

    def test_write_intent_keeps_refusal_copy(self):
        sequence = [ChatResult(content='{"tool":"delete_complaint","args":{"id":1}}')]
        data = self._run('把这条客诉删掉', sequence)
        self.assertEqual(data['answer'], parser.UNSUPPORTED_REPLY)
        self.assertIn('删除或修改', data['answer'])

    def test_record_json_answer_is_humanized(self):
        """模型把记录 JSON 当答案时，不能原样显示给用户。"""
        record = ('{"客户名称":"奥盛精密机械有限公司","提出日期":"2026-09-05",'
                  '"业务员":"马晨刚","工作内容":"提升账期或额度",'
                  '"结果":"有条件通过","id":11}')
        sequence = [
            ChatResult(content='{"tool":"query_records",'
                               '"args":{"module":"customer_complaint","limit":5}}'),
            ChatResult(content=record),
        ]
        data = self._run('月结60天的客户有哪些', sequence)
        self.assertNotIn('"客户名称"', data['answer'])
        self.assertIn('查到的内容', data['answer'])
        self.assertIn('客户名称：奥盛精密机械有限公司', data['answer'])
        self.assertIn('引用卡片', data['answer'])
        self.assertTrue(data['refs'])

    def test_record_json_answer_without_refs_becomes_field_list(self):
        """真实案例：答铜价时模型给 {"铜":"45.69 美元/吨"}，要转成可读文本。"""
        data = self._run('今日铜价是多少', [ChatResult(content='{ "铜": "45.69 美元/吨" }')])
        self.assertNotIn('{"', data['answer'])
        self.assertIn('铜：45.69 美元/吨', data['answer'])

    def test_tool_call_json_as_answer_falls_back_to_refs(self):
        """模型把畸形协议 JSON 当答案时，改列已查到的记录。"""
        sequence = [
            ChatResult(content='{"tool":"query_records",'
                               '"args":{"module":"metal_price","limit":3}}'),
            ChatResult(content='{"error":"BAD_ARGS","hint":"module 参数缺失"}'),
        ]
        data = self._run('今日铜价是多少', sequence)
        self.assertNotIn('"error"', data['answer'])
        self.assertIn('已按你的问题查到以下记录', data['answer'])
        self.assertTrue(data['refs'])

    def test_chinese_tool_key_is_executed(self):
        """{"工具":"query_records","参数":{...}} 应当真的去查，而不是当答案贴出来。"""
        sequence = [
            ChatResult(content='{"工具":"query_records",'
                               '"参数":{"module":"metal_price","limit":3}}'),
            ChatResult(content='铜价 68000 元/吨。'),
        ]
        data = self._run('今日铜价是多少', sequence)
        self.assertEqual(data['answer'], '铜价 68000 元/吨。')
        self.assertEqual(data['steps'][0]['tool'], 'query_records')
        self.assertEqual(data['steps'][0]['module'], 'metal_price')

    def test_plain_answer_is_untouched(self):
        data = self._run('今日铜价是多少', [ChatResult(content='今天铜价 68000 元/吨。')])
        self.assertEqual(data['answer'], '今天铜价 68000 元/吨。')
