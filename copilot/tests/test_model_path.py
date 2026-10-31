"""2026-11-01 配置真实模型后的回归：本地链路与模型链路的接缝。

配了模型之后，"能算的"仍应本地算，"算不了的"才交给模型；但两边各有一批
新暴露出来的毛病：

1. 一次问两个模块（「客诉和诉讼分别有多少」）：本地只答第一个，第二个被
   丢掉——用户看到"客诉 8 条"，以为诉讼也是 0（实际有 1 件）；
2. 「能做什么」没被认出来，交给模型，模型调了两轮 list_modules 之后答
   "这次没有找到符合条件的记录"；
3. 模型拿到 0 行会原地打转（实测 40+ 句"无其他记录。无其他信息。"），
   还会把整段回答包进花括号里显示出来；
4. 槽位把"第一条记录的名字"当成用户给的关键词，下一句追问就换了对象；
5. 「各客户的客诉数量分布」按状态分组——数字全真、维度全错。
"""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from copilot import catalog, registry
from copilot.engine import executor, parser, planner, slots
from copilot.models import CopilotConversation, CopilotPolicy, CopilotRun
from records.models import Complaint, CustomerComplaint

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
User = get_user_model()


@override_settings(CACHES=LOCMEM)
class ModelPathBase(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_superuser('mp_admin', 'mp@x.com', 'p')
        CopilotPolicy.objects.all().delete()
        CopilotPolicy.objects.create(rate_limit_per_10min=100,
                                     rate_limit_per_day=1000, max_tool_rounds=3)
        self.policy = CopilotPolicy.current()
        self.modules = registry.available_modules(self.user, True, self.policy)
        self.conversation = CopilotConversation.objects.create(user=self.user)

    def new_run(self, question='q', conversation=None):
        """同一会话同时只允许一个进行中的 run，所以两次提问要分开建会话。"""
        conv = conversation or CopilotConversation.objects.create(user=self.user)
        return CopilotRun.objects.create(
            conversation=conv, user=self.user, question=question)

    def complaint(self, name, status='未完成', amount='100.00', day=None):
        return CustomerComplaint.objects.create(
            customer_name=name, complaint_date=day or date(2026, 10, 5),
            complaint_status=status, compensation_amount=Decimal(amount))


class CapabilityTests(ModelPathBase):
    """「你能做什么」是确定答案，不该进模型（进去只会换来一句"没找到记录"）。"""

    def test_capability_question_is_answered_locally(self):
        for question in ('你好，你能做什么', '你能干什么', '这个系统能查哪些模块'):
            answer = planner.instant_reply(question, self.modules)
            self.assertIn('我能查的模块', answer, question)

    def test_business_question_is_not_hijacked(self):
        """点了模块名的长句是在问业务，不是问能力。"""
        self.assertEqual(
            planner.instant_reply('供应商能做哪些品类', self.modules), '')


class MultiModuleTests(ModelPathBase):
    """一句问两个模块时，两个都要算，不能只答第一个。"""

    def setUp(self):
        super().setUp()
        self.complaint('甲公司', '未完成', '100')
        self.complaint('乙公司', '已完成', '200')
        Complaint.objects.create(
            dispute_type='买卖合同', defendant='乙公司', case_number='CASE-1',
            amount=Decimal('1000'), court='某法院', lawyer='张律师',
            progress='审理中', filed_at=date(2026, 10, 8))

    def test_two_modules_are_both_planned(self):
        intents = planner.multi_stat_query(
            '客诉和诉讼分别有多少', self.modules, self.user)
        self.assertIsNotNone(intents)
        keys = {i['spec'].key for i in intents}
        self.assertEqual(keys, {'customer_complaint', 'complaint'})

    def test_answer_covers_both_modules(self):
        intents = planner.multi_stat_query(
            '客诉和诉讼分别有多少', self.modules, self.user)
        text, _refs, labels = planner.multi_answer(intents, self.user)
        self.assertIn('客户客诉', text)
        self.assertIn('诉讼', text)
        self.assertEqual(len(labels), 2)

    def test_single_module_question_is_left_alone(self):
        self.assertIsNone(planner.multi_stat_query(
            '客诉有多少条', self.modules, self.user))

    def test_ambiguous_word_is_reported_not_guessed(self):
        """「供应商和客户」里的"客户"对应三个模块：只答一个就要说清楚。"""
        note = planner.ambiguous_note('供应商和客户分别有多少家', self.modules,
                                      covered=['供应商档案'])
        self.assertIn('客户', note)
        self.assertIn('客户台账', note)

    def test_group_dimension_is_not_treated_as_ambiguity(self):
        """「各客户的客诉」里的"客户"是分组维度，不是另一个模块。"""
        self.assertEqual(
            planner.ambiguous_note('各客户的客诉数量分布', self.modules,
                                   covered=['客户客诉']), '')


class GroupDimensionTests(ModelPathBase):
    """「各客户的客诉」要按客户分组，不是按状态。"""

    def test_group_by_customer_name(self):
        stat = planner.stat_query('各客户的客诉数量分布', self.modules, self.user)
        self.assertIsNotNone(stat)
        self.assertEqual(stat['group_field'], 'customer_name')

    def test_module_name_is_not_a_group_dimension(self):
        """"各供应商分别有多少条审厂记录"里的"审厂"是模块名，不是维度：
        按它分组只会得到"每个审厂编号 1 条"。"""
        stat = planner.stat_query('各供应商分别有多少条审厂记录',
                                  self.modules, self.user)
        self.assertEqual(stat['group_field'], '')

    def test_group_answer_drops_redundant_example_column(self):
        self.complaint('甲公司')
        self.complaint('甲公司')
        self.complaint('乙公司')
        stat = planner.stat_query('各客户的客诉数量分布', self.modules, self.user)
        text, _refs = planner.stat_answer(stat, self.user)
        self.assertIn('甲公司', text)
        self.assertNotIn('举例', text, '分组维度就是记录名时，"举例"列是重复的')


class ColloquialSumTests(ModelPathBase):
    """「赔了多少钱」是最自然的口语：接不住就会落到模型手里编数字。"""

    def test_compensation_sum_is_matched(self):
        intent = planner.sum_query('赔了多少钱', self.modules, self.user)
        self.assertIsNotNone(intent)
        self.assertEqual(intent['field'], 'compensation_amount')
        self.assertEqual(intent['label'], '赔偿金额', '单字"赔"不能直接当列名')

    def test_sum_value_matches_database(self):
        self.complaint('甲公司', amount='300')
        self.complaint('乙公司', amount='500')
        intent = planner.sum_query('一共赔了多少钱', self.modules, self.user)
        text, _refs = planner.sum_answer(intent, self.user)
        self.assertIn('800', text)


class DegenerateAnswerTests(ModelPathBase):
    """模型原地打转 / 把回答包进花括号：都要在落地前处理掉。"""

    def test_braces_are_stripped(self):
        raw = '{ "总共有 **0 家** 月结60天的客户。" }'
        self.assertEqual(parser.tidy_answer(raw), '总共有 **0 家** 月结60天的客户。')

    def test_json_like_text_is_untouched(self):
        raw = '{"客户名称": "甲公司"}'
        self.assertEqual(parser.tidy_answer(raw), raw)

    def test_repetition_is_collapsed(self):
        raw = '第一次说。' + '无其他记录。' * 30
        self.assertTrue(parser.is_degenerate(raw))
        self.assertLess(len(parser.tidy_answer(raw)), len(raw))

    def test_normal_answer_is_untouched(self):
        raw = '共 **3 条**。\n\n- 甲\n- 乙\n- 丙\n\n> 统计口径：本月'
        self.assertFalse(parser.is_degenerate(raw))
        self.assertEqual(parser.tidy_answer(raw), raw)

    def test_empty_record_json_falls_back(self):
        answer = executor._readable_answer(
            '{"客户名称": "", "责任业务": ""}', [], self.modules)
        self.assertIn('没有找到', answer)

    def test_single_long_field_is_used_as_answer(self):
        text = ('共 **3 条**，最近一条是 10-16 的甲公司客诉，状态未完成，'
                '责任业务员是李娜，赔偿金额 2000 元。')
        answer = executor._readable_answer('{"客户客诉": "%s"}' % text, [], self.modules)
        self.assertEqual(answer, text)


class SlotKeywordTests(ModelPathBase):
    """槽位只记"用户自己说出来的值"，不能把第一条记录的名字当成条件。"""

    def ref(self, label):
        return [{'module': 'customer_complaint', 'label': label, 'id': 1}]

    def test_first_row_name_is_not_remembered(self):
        run = self.new_run('有哪些未完成的客诉', self.conversation)
        executor._remember_slots(run, [{'tool': 'query_records',
                                        'module': 'customer_complaint'}],
                                 self.ref('【演示】长通精密五金厂'))
        self.conversation.refresh_from_db()
        self.assertNotIn('keyword', self.conversation.slots)
        self.assertEqual(self.conversation.slots.get('module'), 'customer_complaint')

    def test_named_customer_is_remembered(self):
        run = self.new_run('恒信汽车零部件的客诉有哪些', self.conversation)
        executor._remember_slots(run, [{'tool': 'query_records',
                                        'module': 'customer_complaint'}],
                                 self.ref('【演示】恒信汽车零部件公司'))
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.slots.get('keyword'),
                         '【演示】恒信汽车零部件公司')

    def test_short_question_inherits_last_module(self):
        """"赔了多少钱"没点模块，但上一轮在聊客诉：要接上，不能让模型重猜。"""
        slots_data = {'module': 'customer_complaint'}
        self.assertTrue(slots.is_followup('赔了多少钱', slots_data, self.modules))
        text, used = slots.expand('赔了多少钱', slots_data, self.modules)
        self.assertTrue(used)
        self.assertIn('客户客诉', text)

    def test_module_word_means_new_topic(self):
        slots_data = {'module': 'customer_complaint'}
        self.assertFalse(slots.is_followup('保险有多少份', slots_data, self.modules))
