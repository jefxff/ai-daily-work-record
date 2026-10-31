"""模块覆盖与「无数据问题」快路径。

锁住 2026-10-31 用户截图反馈的两个现象（本地模型，徽章「本地模型」）：

1. 问「我问题今天是哪一天」→ 助手去跨表搜"今天"，0 条 → 重复调用被跳过 2 次
   → 「抱歉，这次没能把结果整理成回答。」
2. 问「试样管理中 试样中的记录有那些」→ 「当前没有找到与"试样"相关的记录。」

两者都不是模型质量问题的延续，而是代码/配置问题：
- 日期时间是确定值，根本不该进模型；
- 「试样管理」在系统里是一个完整业务模块（``samples`` app），但 Copilot 的模块
  目录没接入它——模型只能拿模块名当关键词搜，搜不到就答"没有记录"，
  把"我没接这个模块"说成了"系统里没有这条数据"。
"""

import json
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Permission, Role
from ai_api.models import AiProviderConfig
from copilot import catalog, registry
from copilot.engine import planner
from copilot.models import CopilotConversation, CopilotPolicy, CopilotRun
from copilot.providers.base import ChatResult
from samples.models import AuditRecord, Partner, Sample, TestRecord

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
def _plain(text):
    """回答里的 markdown 记号（**加粗**）去掉，只比对人话。"""
    return (text or '').replace('**', '')

User = get_user_model()
MODEL = 'test-model'


def grant(user, *codes):
    role = Role.objects.create(code=f'r_{user.pk}_{abs(hash(codes))}', name='测试角色')
    for code in codes:
        permission = Permission.objects.filter(code=code).first()
        if permission:
            role.permissions.add(permission)
    role.users.add(user)
    return role


class SampleModuleTests(TestCase):
    """试样管理的 5 个实体都应出现在 AI 的模块目录里。"""

    def setUp(self):
        self.admin = User.objects.create_superuser('s_admin', 's@x.com', 'p')
        self.policy = CopilotPolicy()
        self.conversation = CopilotConversation.objects.create(user=self.admin)
        self.run = CopilotRun.objects.create(
            conversation=self.conversation, user=self.admin, question='q')

    def test_samples_app_is_covered(self):
        keys = [m.key for m in registry.available_modules(self.admin, True, self.policy)]
        for key in ('sample', 'sample_test', 'sample_batch', 'sample_audit',
                    'sample_partner'):
            self.assertIn(key, keys)

    def test_perm_key_maps_to_samples_permission(self):
        spec = catalog.get_module('sample_test')
        self.assertEqual(catalog.perm_of(spec), 'samples')

    def test_samples_permission_grants_only_samples_modules(self):
        user = User.objects.create_user('s_only', password='p')
        grant(user, 'samples.read', 'copilot.read')
        keys = [m.key for m in registry.available_modules(user, True, self.policy)]
        self.assertEqual(sorted(keys), sorted(
            ['sample', 'sample_test', 'sample_batch', 'sample_audit', 'sample_partner']))

    def test_soft_deleted_sample_is_invisible(self):
        partner = Partner.objects.create(name='样本供应商')
        alive = Sample.objects.create(
            sample_no='S001', material_name='铝合金', model='A1',
            sample_date=date(2026, 9, 1), supplier=partner)
        Sample.objects.create(
            sample_no='S002', material_name='已删样品', model='A2',
            sample_date=date(2026, 9, 2), supplier=partner, is_deleted=True)
        ctx = registry.ToolContext(
            user=self.admin, conversation=self.conversation, run=self.run,
            is_local=True, policy=self.policy)
        result = registry.run_tool(registry.get('query_records'), ctx,
                                   {'module': 'sample', 'limit': 20})
        self.assertTrue(result.ok)
        ids = [row['id'] for row in result.data['rows']]
        self.assertEqual(ids, [alive.pk])


class InstantReplyTests(TestCase):
    """不需要数据就能确定回答的问题，一律本地答，不进模型。"""

    def setUp(self):
        self.modules = list(catalog.MODULES)
        self.admin = User.objects.create_superuser('i_admin', 'i@x.com', 'p')

    def test_date_question_answered_locally(self):
        for question in ('今天是哪一天', '我问题今天是哪一天', '今天几号', '今天星期几'):
            reply = planner.instant_reply(question, self.modules)
            self.assertIn('今天是', reply, question)
            self.assertIn('星期', reply, question)

    def test_date_answer_matches_local_date(self):
        from django.utils import timezone
        today = timezone.localdate()
        reply = planner.instant_reply('今天几号', self.modules)
        self.assertIn(f'{today.year}年', reply)
        self.assertIn(f'{today.month}月{today.day}日', reply)

    def test_time_question_answered_locally(self):
        self.assertIn('现在是', planner.instant_reply('现在几点了', self.modules))

    def test_capability_question_lists_modules(self):
        reply = planner.instant_reply('你能查哪些模块', self.modules)
        self.assertIn('我能查的模块', reply)
        self.assertIn('试样试产', reply)

    def test_business_question_is_not_intercepted(self):
        self.assertEqual(planner.instant_reply('试样有哪些记录', self.modules), '')


class ModuleIntentTests(TestCase):
    """「某模块里有哪些记录」应当直接本地查，而不是拿模块名当关键词搜。"""

    def setUp(self):
        self.modules = list(catalog.MODULES)

    def test_pure_list_question_hits_module(self):
        spec = planner.module_intent('试样管理中 试样中的记录有那些', self.modules)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.key, 'sample_test')

    def test_question_with_real_keyword_goes_to_model(self):
        self.assertIsNone(planner.module_intent('华建建材有哪些客户客诉', self.modules))

    def test_vague_detail_question_goes_to_model(self):
        self.assertIsNone(planner.module_intent('客诉详情', self.modules))

    def test_module_without_list_cue_is_ignored(self):
        self.assertIsNone(planner.module_intent('今天天气不错', self.modules))


@override_settings(CACHES=LOCMEM, COPILOT_INLINE_EXECUTION=True)
class NoModelNeededTests(TestCase):
    """日期问题与纯模块列表问题，模型一次都不应该被调用。"""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_superuser('n_admin', 'n@x.com', 'p')
        self.client.force_login(self.user)
        self.config = AiProviderConfig.objects.create(
            purpose=AiProviderConfig.Purpose.CHAT,
            provider=AiProviderConfig.Provider.OLLAMA,
            model=MODEL, is_enabled=True, is_local=True, max_tokens=256,
        )
        CopilotPolicy.objects.all().delete()
        CopilotPolicy.objects.create(rate_limit_per_10min=100, rate_limit_per_day=1000)
        self.conversation = CopilotConversation.objects.create(user=self.user)
        self.run = CopilotRun.objects.create(
            conversation=self.conversation, user=self.user, question='q')
        partner = Partner.objects.create(name='宏达五金')
        sample = Sample.objects.create(
            sample_no='S100', material_name='不锈钢板', model='S304',
            sample_date=date(2026, 9, 20), supplier=partner)
        TestRecord.objects.create(
            sample=sample, round_no=1, test_date=date(2026, 9, 22),
            test_result='尺寸合格', review_conclusion='pass', report_code='BG-001')
        AuditRecord.objects.create(
            audit_no='AR-001', partner=partner, round_no=1,
            audit_date=date(2026, 9, 1), audit_result=AuditRecord.Result.PASS,
            audit_remark='首轮通过', auditor='张三')
        AuditRecord.objects.create(
            audit_no='AR-002', partner=partner, round_no=2,
            audit_date=date(2026, 9, 10), audit_result=AuditRecord.Result.FAIL,
            audit_remark='整改未完成', auditor='李四')

    def _ask(self, question):
        return self.client.post(
            reverse('copilot:ask'), data=json.dumps({'question': question}),
            content_type='application/json').json()['data']

    def test_date_question_never_calls_model(self):
        with patch('copilot.providers.router.select', return_value=(self.config, None)), \
             patch('copilot.providers.router.chat_with',
                   side_effect=AssertionError('日期问题不应调用模型')):
            data = self._ask('我问题今天是哪一天')
        self.assertEqual(data['status'], 'succeeded')
        self.assertIn('今天是', data['answer'])
        self.assertEqual(data['steps'], [])

    def test_module_list_question_queried_locally(self):
        with patch('copilot.providers.router.select', return_value=(self.config, None)), \
             patch('copilot.providers.router.chat_with',
                   side_effect=AssertionError('模块列表问题不应调用模型')):
            data = self._ask('试样管理中 试样中的记录有那些')
        self.assertEqual(data['status'], 'succeeded')
        self.assertIn('试样试产', data['answer'])
        self.assertIn('共 1 条', _plain(data['answer']))
        self.assertEqual(data['steps'][0]['tool'], 'query_records')
        self.assertEqual(data['steps'][0]['module'], 'sample_test')
        self.assertTrue(data['refs'])

    def test_global_search_on_module_name_returns_its_records(self):
        """关键词是模块名时，直接列出该模块的记录，而不是回 0 条。"""
        ctx = registry.ToolContext(
            user=self.user, conversation=self.conversation, run=self.run,
            is_local=True, policy=CopilotPolicy.current())
        result = registry.run_tool(registry.get('global_search'), ctx,
                                   {'keyword': '试样'})
        self.assertTrue(result.ok)
        self.assertEqual(result.row_count, 1)
        group = result.data['groups'][0]
        self.assertEqual(group['module'], 'sample_test')
        self.assertEqual(group['matched_by'], 'module_name')
        self.assertIn('模块名', result.data['hint'])
        self.assertIn('选项', result.data['hint'])
        self.assertTrue(result.refs)

    def test_audit_negative_wording_maps_to_fail(self):
        """"审厂没通过"要落到 audit_result=fail，而不是 pass。"""
        ctx = registry.ToolContext(
            user=self.user, conversation=self.conversation, run=self.run,
            is_local=True, policy=CopilotPolicy.current())
        result = registry.run_tool(registry.get('query_records'), ctx,
                                   {'module': 'sample_audit',
                                    'keyword': '审厂没通过', 'limit': 10})
        self.assertTrue(result.ok)
        self.assertEqual(result.data['total'], 1)
        self.assertEqual(result.data['rows'][0]['audit_result'], 'fail')

    def test_multi_module_question_picks_the_one_with_choice_word(self):
        """「审厂没通过的供应商」命中两个模块：应按"没通过"选审厂。"""
        modules = registry.available_modules(self.user, True, CopilotPolicy.current())
        spec, keyword, _state = planner.module_query('审厂没通过的供应商有哪些', modules)
        self.assertIsNotNone(spec)
        self.assertEqual(spec.key, 'sample_audit')
        self.assertEqual(keyword, '没通过')

    def test_choice_word_question_answered_locally(self):
        """选项词问题也走本地确定性查询，不交给模型猜。"""
        with patch('copilot.providers.router.select', return_value=(self.config, None)), \
             patch('copilot.providers.router.chat_with',
                   side_effect=AssertionError('选项筛选问题不应调用模型')):
            data = self._ask('审厂没通过的供应商有哪些')
        self.assertEqual(data['status'], 'succeeded')
        self.assertIn('审厂', data['answer'])
        self.assertIn('共 1 条', _plain(data['answer']))
        self.assertEqual(data['steps'][0]['module'], 'sample_audit')
        self.assertTrue(data['refs'])

    def test_no_data_reply_lists_modules(self):
        """模型没答案也没数据时，说清能查什么，而不是「没能把结果整理成回答」。"""
        sequence = [
            ChatResult(content='{"tool":"global_search","args":{"keyword":"试样"}}'),
            ChatResult(content='{"tool":"global_search","args":{"keyword":"试样"}}'),
            ChatResult(content='{"tool":"global_search","args":{"keyword":"试样"}}'),
        ]
        with patch('copilot.providers.router.select', return_value=(self.config, None)), \
             patch('copilot.providers.router.chat_with',
                   side_effect=lambda *a, **k: sequence.pop(0) if sequence else
                   ChatResult(content='{"ok":false}')):
            data = self._ask('有没有跟试样有关的东西')
        self.assertNotIn('没能把结果整理成回答', data['answer'])
        self.assertTrue(
            '我可以查的模块' in data['answer'] or '试样试产' in data['answer'],
            data['answer'])
