"""统计类问题的确定性与「不许编数」。

锁住 2026-10-31 用户截图反馈的两条严重错误答案（本地模型，徽章「本地模型」）：

1. 问「诉讼中立案的有哪些？结案的有哪些」→ 只回了立案的 3 条，而且是把
   ``{"立案的案件": [...], "立案的被告": [...]}`` 当"一条记录"展开成一串
   Python 列表；结案那一半完全没答。
2. 问「客户台账中账期为月结60天的客户一共有多少？」→ 答「一共有 50 个，
   其中普通客户 32 个，代加工客户 18 个」。50/32/18 都是真数，但那是**全表**
   的统计——模型调 ``aggregate_records(group_by=customer_type)`` 时没带任何
   筛选条件，把"全表分布"当成了"月结60天的分布"。真值是 12 家。

共性：这类问题的答案是**算出来的**。让模型自己决定统计口径，它会把口径搞丢；
交给本地确定性路径，既快又不会编。
"""

import json
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from ai_api.models import AiProviderConfig
from copilot import catalog, registry
from copilot.engine import executor, planner
from copilot.models import CopilotConversation, CopilotPolicy, CopilotRun
from copilot.providers.base import ChatResult
from records.models import Complaint, CustomerComplaint, CustomerLedger

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
def _plain(text):
    """回答里的 markdown 记号（**加粗**）去掉，只比对人话。"""
    return (text or '').replace('**', '')

User = get_user_model()


class StatsBase(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_superuser('st_admin', 'st@x.com', 'p')
        CopilotPolicy.objects.all().delete()
        CopilotPolicy.objects.create(rate_limit_per_10min=100, rate_limit_per_day=1000,
                                     max_tool_rounds=3)
        self.policy = CopilotPolicy.current()
        self.modules = registry.available_modules(self.admin, True, self.policy)
        # 工具留痕要求非空 conversation/run，否则会刷 IntegrityError 噪音
        self.conversation = CopilotConversation.objects.create(user=self.admin)
        self.run = CopilotRun.objects.create(
            conversation=self.conversation, user=self.admin, question='q')

    def tool_ctx(self):
        return registry.ToolContext(
            user=self.admin, conversation=self.conversation, run=self.run,
            is_local=True, policy=self.policy)

    def ledger(self, name, terms, category='生产型'):
        return CustomerLedger.objects.create(
            customer_name=name, payment_terms=terms, customer_category=category)


class KeywordFilterTests(StatsBase):
    """分组维度也要能被 keyword 命中，否则"月结60天"永远查不到。"""

    def test_payment_terms_is_searchable(self):
        self.ledger('甲公司', '月结60天')
        self.ledger('乙公司', '月结90天')
        spec = catalog.get_module('customer_ledger')
        self.assertIn('payment_terms', catalog.search_fields(spec))

    def test_aggregate_counts_filtered_rows(self):
        for index in range(3):
            self.ledger(f'甲{index}', '月结60天')
        self.ledger('乙公司', '货到付款')
        ctx = self.tool_ctx()
        result = registry.run_tool(
            registry.get('aggregate_records'), ctx,
            {'module': 'customer_ledger', 'metric': 'count', 'keyword': '月结60天'})
        self.assertTrue(result.ok)
        self.assertEqual(result.data['rows'][0]['value'], 3)
        self.assertEqual(result.data['keyword'], '月结60天')
        self.assertIn('筛选', result.data['scope'])
        self.assertNotIn('note', result.data)

    def test_aggregate_without_keyword_says_it_is_whole_table(self):
        self.ledger('甲公司', '月结60天')
        ctx = self.tool_ctx()
        result = registry.run_tool(
            registry.get('aggregate_records'), ctx,
            {'module': 'customer_ledger', 'metric': 'count', 'group_by': 'customer_category'})
        self.assertTrue(result.ok)
        self.assertEqual(result.data['keyword'], '')
        self.assertIn('全表', result.data['scope'])
        # 必须明确告诉模型"这不是用户条件的结果"，否则它会直接把全表数字答出去
        self.assertIn('全表', result.data['note'])


class StatQueryTests(StatsBase):
    def test_count_uses_value_found_in_database(self):
        for index in range(4):
            self.ledger(f'甲{index}', '月结60天')
        self.ledger('乙公司', '月结90天')
        stat = planner.stat_query('客户台账中账期为月结60天的客户一共有多少？',
                                  self.modules, self.admin)
        self.assertIsNotNone(stat)
        self.assertEqual(stat['spec'].key, 'customer_ledger')
        self.assertEqual(stat['keyword'], '月结60天')
        answer, refs = planner.stat_answer(stat, self.admin, self.policy)
        self.assertIn('共 4 家', _plain(answer))
        self.assertTrue(refs)

    def test_group_by_when_two_labels_of_same_field(self):
        Complaint.objects.create(dispute_type='买卖合同纠纷', defendant='甲公司',
                                 case_number='A-1', amount=Decimal('1000'),
                                 court='某院', lawyer='张三', progress='立案',
                                 filed_at=date(2026, 1, 5))
        Complaint.objects.create(dispute_type='买卖合同纠纷', defendant='乙公司',
                                 case_number='A-2', amount=Decimal('2000'),
                                 court='某院', lawyer='张三', progress='结案',
                                 filed_at=date(2026, 2, 5))
        stat = planner.stat_query('诉讼中立案的有哪些？结案的有哪些',
                                  self.modules, self.admin)
        self.assertIsNotNone(stat)
        self.assertEqual(stat['group_field'], 'progress')
        self.assertEqual(stat['keyword'], '')
        answer, refs = planner.stat_answer(stat, self.admin, self.policy)
        # 两半都要答，而且数字来自库，不是编的
        self.assertIn('| 立案 | 1 件 |', answer)
        self.assertIn('| 结案 | 1 件 |', answer)
        self.assertEqual(len(refs), 2)

    def test_modifier_on_one_status_filters_instead_of_grouping(self):
        Complaint.objects.create(dispute_type='买卖合同纠纷', defendant='甲公司',
                                 case_number='A-1', amount=Decimal('1000'),
                                 court='某院', lawyer='张三', progress='审理中')
        stat = planner.stat_query('审理中的诉讼一共有多少条', self.modules, self.admin)
        self.assertIsNotNone(stat)
        self.assertEqual(stat['keyword'], '审理中')
        answer, _refs = planner.stat_answer(stat, self.admin, self.policy)
        self.assertIn('共 1 件', _plain(answer))

    def test_ledger_wins_over_credit_when_value_belongs_to_ledger(self):
        """「账期为月结90天」里的"账期"会命中"客户账期"，但值是台账的字段。"""
        self.ledger('甲公司', '月结90天')
        stat = planner.stat_query('账期是月结90天的客户有几家', self.modules, self.admin)
        self.assertIsNotNone(stat)
        self.assertEqual(stat['spec'].key, 'customer_ledger')

    def test_relative_scope_is_computed_locally(self):
        """「本月/上个月」日历上无歧义：换算成区间本地算，不再推给模型。"""
        self.ledger('甲公司', '月结60天')
        for question in ('上个月客诉有多少条', '本月客诉有多少条'):
            stat = planner.stat_query(question, self.modules, self.admin)
            self.assertIsNotNone(stat, question)
            self.assertIsNotNone(stat['date'], question)
            self.assertIn('月', stat['date'][1])

    def test_price_question_is_not_a_count(self):
        """「今日铜价是多少」问取值，不能被当成计数。"""
        self.assertIsNone(planner.stat_query('今日铜价是多少', self.modules, self.admin))

    def test_list_question_with_value_lists_records(self):
        self.ledger('甲公司', '月结60天')
        spec, keyword, state = planner.module_query('月结60天的客户有那些',
                                                    self.modules, self.admin)
        self.assertEqual(spec.key, 'customer_ledger')
        self.assertEqual(keyword, '月结60天')
        self.assertIsNone(state)


class ListJsonAnswerTests(TestCase):
    """含数组的 JSON 不是"一条记录"，不能原样铺开。"""

    def test_array_values_are_not_rendered(self):
        text = ('{"立案的案件": ["买卖合同纠纷", "买卖合同纠纷"],'
                '"立案的被告": ["甲公司", "乙公司"]}')
        answer = executor._readable_answer(text, [
            {'module': 'complaint', 'module_label': '诉讼', 'id': 1, 'label': '甲公司'},
            {'module': 'complaint', 'module_label': '诉讼', 'id': 2, 'label': '乙公司'},
        ], ())
        self.assertNotIn('["', answer)
        self.assertIn('已按你的问题查到以下记录', answer)

    def test_flat_record_still_rendered(self):
        answer = executor._readable_answer('{"金属名称":"铜","价格":"68000"}', [], ())
        self.assertIn('金属名称：铜', answer)


@override_settings(CACHES=LOCMEM, COPILOT_INLINE_EXECUTION=True)
class StatNoModelTests(TestCase):
    """统计问题不该再经过模型——一旦经过，就有编数字的机会。"""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_superuser('st_api', 'st2@x.com', 'p')
        AiProviderConfig.objects.create(
            purpose='chat', provider='ollama', model='test-model', is_enabled=True,
            is_local=True, max_tokens=256)
        CopilotPolicy.objects.all().delete()
        CopilotPolicy.objects.create(rate_limit_per_10min=100, rate_limit_per_day=1000,
                                     max_tool_rounds=3)

    def test_count_question_skips_model(self):
        for index in range(5):
            CustomerLedger.objects.create(customer_name=f'甲{index}', payment_terms='月结60天')
        CustomerLedger.objects.create(customer_name='乙公司', payment_terms='货到付款')
        with patch('copilot.providers.router.chat_with') as chat:
            data = self._ask('客户台账中账期为月结60天的客户一共有多少？')
        chat.assert_not_called()
        self.assertIn('共 5 家', _plain(data['answer']))
        self.assertEqual(data['steps'][0]['tool'], 'aggregate_records')

    def test_group_question_lists_both_groups(self):
        for index, progress in enumerate(['立案', '立案', '结案']):
            Complaint.objects.create(
                dispute_type='买卖合同纠纷', defendant=f'被告{index}',
                case_number=f'C-{index}', amount=Decimal('1000'),
                court='某院', lawyer='张三', progress=progress,
                filed_at=date(2026, 3, index + 1))
        with patch('copilot.providers.router.chat_with') as chat:
            data = self._ask('诉讼中立案的有哪些？结案的有哪些')
        chat.assert_not_called()
        self.assertIn('| 立案 | 2 件 |', data['answer'])
        self.assertIn('| 结案 | 1 件 |', data['answer'])

    def test_list_question_still_lists_records(self):
        CustomerComplaint.objects.create(
            customer_name='华建建材', complaint_date=date(2026, 9, 10),
            complaint_status='处理中')
        with patch('copilot.providers.router.chat_with', return_value=ChatResult(
                content='华建建材共有 1 条客诉，目前处理中。')):
            data = self._ask('华建建材的客诉')
        self.assertIn('华建建材', data['answer'])

    def _ask(self, question):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('copilot:ask'), data=json.dumps({'question': question}),
            content_type='application/json')
        return response.json()['data']
