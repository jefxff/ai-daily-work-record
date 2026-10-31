"""2026-10-31 评审回归：四类"看着答了、其实答错"的问句。

这些都不是模型质量问题——本地链路明明能算/能筛，却在半路把条件丢了：

1. 残余文本当关键词：「最近过期的证照有哪些」把"最近过期"整串拿去 icontains，
   库里 5 条已过期证照一条都查不到，答成"没有记录"；
2. 否定词只看一个字：「审厂没有通过的供应商」里"通过"前面是"有"，被判成正说；
3. 状态词纠偏过头：「华建建材未完成的客诉」的"未完成"是客诉的**选项值**，
   却被当成每日记录的状态，整句话改判过去，答"每日记录 共 11 条"；
4. 相对口径不落地：「上个月客诉有多少」解析不出区间就推给模型，模型没配
   就降级成"没有记录"（真值 12 条）。

另加两条同批修的：证照"需要年审"没有口径、提示词占位符被照抄成脏步骤。
"""

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from archive.models import License
from copilot import catalog, registry
from copilot.engine import executor, planner, prompt
from copilot.models import CopilotConversation, CopilotPolicy, CopilotRun
from records.models import Complaint, CustomerComplaint
from samples.models import AuditRecord, Partner

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
User = get_user_model()


@override_settings(CACHES=LOCMEM)
class RegressionBase(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_superuser('rg_admin', 'rg@x.com', 'p')
        CopilotPolicy.objects.all().delete()
        CopilotPolicy.objects.create(rate_limit_per_10min=100,
                                     rate_limit_per_day=1000, max_tool_rounds=3)
        self.policy = CopilotPolicy.current()
        self.modules = registry.available_modules(self.user, True, self.policy)
        self.conversation = CopilotConversation.objects.create(user=self.user)
        self.run = CopilotRun.objects.create(
            conversation=self.conversation, user=self.user, question='q')
        self.today = timezone.localdate()

    def ctx(self):
        return registry.ToolContext(
            user=self.user, conversation=self.conversation, run=self.run,
            is_local=True, policy=self.policy)

    def query(self, **args):
        result = registry.run_tool(registry.get('query_records'), self.ctx(), args)
        self.assertTrue(result.ok, result.error)
        return result.data


class NegationTests(RegressionBase):
    """否定词必须跨字匹配："没有通过"里"通过"前是"有"，只往前看一个字就反了。"""

    def test_phrase_negated_knows_multi_char_words(self):
        for text in ('没有通过', '并非通过', '从未通过', '尚未通过', '审厂没通过'):
            index = text.find('通过')
            self.assertTrue(catalog.phrase_negated(text, index, '通过'), text)
        # 正说与"本身就是否定式"的短语不算反说
        self.assertFalse(catalog.phrase_negated('已经通过了', 2, '通过'))
        self.assertFalse(catalog.phrase_negated('未通过', 0, '未通过'))

    def test_no_pass_supplier_returns_fail_only(self):
        partner = Partner.objects.create(name='宏达五金')
        AuditRecord.objects.create(
            audit_no='AR-1', partner=partner, round_no=1, audit_date=date(2026, 9, 1),
            audit_result=AuditRecord.Result.PASS, auditor='张三')
        AuditRecord.objects.create(
            audit_no='AR-2', partner=partner, round_no=2, audit_date=date(2026, 9, 10),
            audit_result=AuditRecord.Result.FAIL, auditor='李四')
        data = self.query(module='sample_audit', keyword='没有通过', limit=10)
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['rows'][0]['audit_result'], 'fail')


class ResidualKeywordTests(RegressionBase):
    """状态词剥离后的修饰语不是关键词——带着检索只会 0 条。"""

    def test_expired_license_ignores_modifier(self):
        License.objects.create(name='已到期证', code='LIC-R1',
                               expire_date=self.today - timedelta(days=10))
        License.objects.create(name='已到期证二', code='LIC-R2',
                               expire_date=self.today - timedelta(days=3))
        License.objects.create(name='正常证', code='LIC-R3',
                               expire_date=self.today + timedelta(days=400))
        spec, keyword, state = planner.module_query(
            '最近过期的证照有哪些', self.modules, self.user)
        self.assertEqual(spec.key, 'license')
        self.assertEqual(keyword, '', '「最近」这类修饰语不该变成检索词')
        self.assertIsNotNone(state)
        data = self.query(module=spec.key, limit=10, state=state.key, keyword=keyword)
        self.assertEqual(data['total'], 2)


class RetargetTests(RegressionBase):
    """选项值也是"这个模块接得住"，不能据此把主语改判到别的模块。"""

    def test_choice_value_keeps_the_named_module(self):
        spec, _keyword, _state = planner.module_query(
            '华建建材未完成的客诉', self.modules, self.user)
        self.assertEqual(spec.key, 'customer_complaint',
                         '「未完成」是客诉的选项值，不该改判成每日记录')

    def test_choice_and_customer_are_combined_with_and(self):
        CustomerComplaint.objects.create(
            customer_name='华建建材', complaint_date=self.today,
            complaint_status='未完成', compensation_amount=Decimal('100'))
        CustomerComplaint.objects.create(
            customer_name='别家公司', complaint_date=self.today,
            complaint_status='未完成', compensation_amount=Decimal('100'))
        CustomerComplaint.objects.create(
            customer_name='华建建材', complaint_date=self.today,
            complaint_status='已完成', compensation_amount=Decimal('100'))
        data = self.query(module='customer_complaint',
                          keyword='华建建材未完成', limit=10)
        self.assertEqual(data['total'], 1, '客户与状态必须是"与"的关系')
        self.assertEqual(data['rows'][0]['customer_name'], '华建建材')
        self.assertEqual(data['rows'][0]['complaint_status'], '未完成')


class ReviewDueTests(RegressionBase):
    """「有哪些需要年审」= 年审日已到点，不是"30 天内"，更不是全表。"""

    def test_needs_review_uses_next_review_date(self):
        License.objects.create(name='该年审的', code='LIC-V1',
                               expire_date=self.today + timedelta(days=400),
                               next_review_date=self.today - timedelta(days=5))
        License.objects.create(name='年审还早', code='LIC-V2',
                               expire_date=self.today + timedelta(days=400),
                               next_review_date=self.today + timedelta(days=200))
        spec, keyword, state = planner.module_query(
            '证照管理中，有哪些需要年审', self.modules, self.user)
        self.assertEqual(spec.key, 'license')
        self.assertEqual(keyword, '')
        self.assertEqual(getattr(state, 'key', None), 'review_due')
        data = self.query(module=spec.key, limit=10, state=state.key)
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['rows'][0]['name'], '该年审的')

    def test_pending_review_still_means_soon(self):
        """「要年审了」仍是"30 天内"那档，别被"要年审"吞掉。"""
        spec, _keyword, state = planner.module_query(
            '证照里要年审了的有哪些', self.modules, self.user)
        self.assertEqual(spec.key, 'license')
        self.assertEqual(getattr(state, 'key', None), 'pending_review')


class ConditionGuardTests(RegressionBase):
    def test_choice_word_guarded_as_option_not_as_unsupported(self):
        """"未完成"是客诉的选项值，不能说成"没有口径"（下一句还列着它）。"""
        stub = planner.unsupported_condition(
            '华建建材未完成的客诉', self.modules, self.user)
        self.assertIn('可以按选项直接筛', stub)
        self.assertNotIn('没有对应的筛选口径', stub)


class StepNoiseTests(RegressionBase):
    """占位符不是工具调用，别把它写成一条看不懂的步骤。"""
    def test_placeholder_tool_name_is_not_recorded(self):
        steps = []
        executor._unknown_tool(steps, {}, '工具名')
        self.assertEqual(steps, [])

    def test_real_unknown_tool_is_recorded_once(self):
        steps = []
        executor._unknown_tool(steps, {}, 'export_excel')
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]['tool'], 'export_excel')

    def test_prompt_shows_a_real_tool_name(self):
        text = prompt.build_system_prompt(
            self.modules, registry.available_specs(self.user, True, self.policy),
            today=self.today.isoformat())
        self.assertIn('"tool":"query_records"', text)
        self.assertNotIn('"tool":"工具名"', text)


class FieldValuePhraseTests(RegressionBase):
    """「进度为判决」这类"字段名+系词+选项值"句式，两条路径都得答对。

    「诉讼中进度为判决的有哪些」曾答"没有记录"而「有几条」答对 4 条：
    统计路径把选项值精确翻译成 ``progress='判决'``，列表路径却把剥完标签
    剩下的"进度为"拿去 icontains——任何记录里都没有这三个字，0 条。
    """

    def complaint(self, case, progress='判决', court='区人民法院'):
        return Complaint.objects.create(
            dispute_type='质量责任纠纷', defendant='被告方', case_number=case,
            court=court, progress=progress, amount=Decimal('1000'),
            filed_at=date(2026, 10, 8))

    def test_field_prefix_and_copula_stripped(self):
        self.complaint('案-1')
        self.complaint('案-2', progress='审理中')
        spec = catalog.get_module('complaint')
        from copilot.tools.common import _strip_choice_labels, apply_filters
        self.assertEqual(_strip_choice_labels(spec, '进度为判决'), '')
        qs = catalog.model_of(spec).objects.all()
        self.assertEqual(apply_filters(qs, spec, keyword='进度为判决').count(), 1)
        # 泛称前缀同理："状态为审理中"不能剩个"状态"去检索
        self.assertEqual(apply_filters(qs, spec, keyword='状态为审理中').count(), 1)

    def test_list_and_count_agree_on_choice_value(self):
        self.complaint('案-1')
        self.complaint('案-2')
        self.complaint('案-3', progress='审理中')
        data = self.query(module='complaint', keyword='进度为判决', limit=10)
        self.assertEqual(data['total'], 2)

    def test_trailing_particle_does_not_break_filter(self):
        """「进度为判决有几条」的残余"进度为判决有"剩个"有"会把 4 条筛成 0。"""
        self.complaint('案-1')
        self.complaint('案-2')
        data = self.query(module='complaint', keyword='进度为判决有', limit=10)
        self.assertEqual(data['total'], 2)

    def test_negated_value_excludes(self):
        """「不是判决」应翻成排除，而不是当没条件或答反。"""
        self.complaint('案-1')
        self.complaint('案-2', progress='执行')
        data = self.query(module='complaint', keyword='不是判决', limit=10)
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['rows'][0]['progress'], '执行')

    def test_stat_count_respects_negation(self):
        intent = None
        self.complaint('案-1')
        self.complaint('案-2', progress='执行')
        intent = planner.stat_query('诉讼中不是判决的有几条', self.modules,
                                    self.user)
        self.assertIsNotNone(intent)
        text, _refs = planner.stat_answer(intent, self.user)
        self.assertIn('1', text)
        self.assertNotIn('「判决」的共 **2', text)
