"""计数排名（「哪家客户客诉最多」）与状态纠偏（「试样管理中已终止」）的回归。

这两类问题以前都会落到"不装懂"守卫上，守卫话术本身没错，可用户要的
明明是个能算的答案：

1. **计数排名**：「哪家客户客诉最多」的度量是条数，模块里没有"客诉数量"
   字段，rank_query 的数值排名接不住，以前答"没有数值字段可比较"；
2. **状态纠偏**：「试样管理中已终止」的"试样"命中试样试产，可"已终止"
   是样品送样的状态——以前答"试样试产没有这个口径"；
3. **否定词**：「没通过」里也含"通过"，以前的状态匹配会把反话说成正话。
"""

from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from copilot import catalog
from copilot.engine import planner
from copilot.tools.daterange import extract_relative_range
from records.models import CustomerComplaint

User = get_user_model()


def _module(key):
    return next(m for m in catalog.MODULES if m.key == key)


class RelativeRangeTests(TestCase):
    """本月/上个月/今年/上周：日历上无歧义的相对口径要能换算成区间。"""

    def test_last_month_crosses_year(self):
        found = extract_relative_range('上个月', today=date(2026, 1, 15))
        self.assertEqual((found.start, found.end), (date(2025, 12, 1), date(2025, 12, 31)))

    def test_common_words(self):
        cases = {
            '上个月': (date(2026, 9, 1), date(2026, 9, 30)),
            '上月': (date(2026, 9, 1), date(2026, 9, 30)),
            '本月': (date(2026, 10, 1), date(2026, 10, 31)),
            '今年': (date(2026, 1, 1), date(2026, 12, 31)),
            '去年': (date(2025, 1, 1), date(2025, 12, 31)),
        }
        for word, (start, end) in cases.items():
            with self.subTest(word=word):
                found = extract_relative_range(word, today=date(2026, 10, 31))
                self.assertEqual((found.start, found.end), (start, end))

    def test_week_bounds(self):
        # 2026-10-31 是周六：本周从 10-26（周一）起，上周是 10-19 ~ 10-25
        found = extract_relative_range('上周', today=date(2026, 10, 31))
        self.assertEqual((found.start, found.end), (date(2026, 10, 19), date(2026, 10, 25)))
        found = extract_relative_range('本周', today=date(2026, 10, 31))
        self.assertEqual((found.start, found.end), (date(2026, 10, 26), date(2026, 11, 1)))

    def test_no_relative_word(self):
        self.assertIsNone(extract_relative_range('2026年9月'))
        self.assertIsNone(extract_relative_range('最近30天'))
        self.assertIsNone(extract_relative_range('客诉有哪些'))


class TopCountTests(TestCase):
    """「哪家客户客诉最多」：按客户分组计数，而不是找数值字段。"""

    def setUp(self):
        self.admin = User.objects.create_superuser('topcount_admin', 't@x.com', 'p')
        self.modules = catalog.MODULES
        last_month = extract_relative_range('上个月')
        this_month = extract_relative_range('本月')
        # 上个月：甲 2 条、乙 1 条；本月：丙 3 条（证明时间口径真的生效）
        for customer, day, count in (
            ('甲客户', last_month.start, 2),
            ('乙客户', last_month.start + timedelta(days=5), 1),
            ('丙客户', this_month.start + timedelta(days=2), 3),
        ):
            for offset in range(count):
                CustomerComplaint.objects.create(
                    customer_name=customer,
                    complaint_date=day + timedelta(days=offset),
                    complaint_status='处理中')

    def test_last_month_scope_is_honored(self):
        intent = planner.top_count_query('上个月哪家客户客诉最多', self.modules, self.admin)
        self.assertIsNotNone(intent, '计数排名应被接住，而不是落到"无数值字段"守卫')
        self.assertEqual(intent['spec'].key, 'customer_complaint')
        self.assertEqual(intent['group_field'], 'customer_name')
        self.assertIsNotNone(intent['date'], '"上个月"必须换算成区间')
        answer, _refs = planner.top_count_answer(intent, self.admin)
        self.assertIn('甲客户', answer)
        self.assertIn('2 条', answer.replace('**', ''))
        self.assertNotIn('丙客户', answer, '本月的 3 条不能混进上个月的排名')

    def test_overall_champion_without_time_scope(self):
        intent = planner.top_count_query('客诉最多的客户是哪个', self.modules, self.admin)
        self.assertIsNotNone(intent)
        self.assertIsNone(intent['date'])
        answer, refs = planner.top_count_answer(intent, self.admin)
        self.assertIn('丙客户', answer)
        self.assertIn('3 条', answer.replace('**', ''))
        self.assertTrue(refs, '榜首要带明细卡片')

    def test_numeric_rank_still_wins(self):
        """点到真正的数值字段时让给 rank_query，不做计数排名。"""
        CustomerComplaint.objects.create(
            customer_name='甲客户', complaint_date=timezone.localdate(),
            complaint_status='处理中', compensation_amount=100)
        self.assertIsNone(
            planner.top_count_query('赔偿金额最多的客诉是哪条', self.modules, self.admin))
        rank = planner.rank_query('赔偿金额最多的客诉是哪条', self.modules, self.admin)
        self.assertIsNotNone(rank)
        self.assertEqual(rank['field'], 'compensation_amount')

    def test_unrelated_question_not_claimed(self):
        """没有分组维度可认时不要抢：让原有分支（守卫/模型）继续兜底。"""
        self.assertIsNone(
            planner.top_count_query('最长的一条规定是什么', self.modules, self.admin))


class RetargetStateTests(TestCase):
    """「试样管理中已终止」：状态词是别的模块的，主语要纠过去。"""

    def setUp(self):
        self.admin = User.objects.create_superuser('retarget_admin', 'r@x.com', 'p')
        self.modules = catalog.MODULES

    def test_condition_word_retargets_module(self):
        spec, keyword, state = planner.module_query(
            '试样管理中已终止的有哪些', self.modules, self.admin)
        self.assertEqual(spec.key, 'sample', '"已终止"是样品送样的状态')
        self.assertEqual(state.key, 'terminated')
        self.assertEqual(keyword, '', '状态词不能同时当关键词双重过滤')

    def test_count_question_retargets_too(self):
        spec, _keyword, state = planner.module_query(
            '试样管理中已终止的有几条', self.modules, self.admin)
        self.assertEqual(spec.key, 'sample')
        self.assertEqual(state.key, 'terminated')

    def test_ambiguous_word_does_not_retarget(self):
        """"通过"试样试产/小批量都有：两个模块都认识就不猜。"""
        fixed_spec, _state = planner._retarget_state(
            _module('decoration'), '装修中通过的有哪些', self.modules)
        self.assertIsNone(fixed_spec)

    def test_own_state_not_retargeted(self):
        spec, _keyword, state = planner.module_query(
            '试样试产中没通过的有哪些', self.modules, self.admin)
        self.assertEqual(spec.key, 'sample_test')
        self.assertEqual(state.key, 'failed', '"没通过"是否定，不能读成"通过"')


class NegationStateTests(TestCase):
    """状态短语匹配要认否定词：反话不能读成正话。"""

    def test_negated_phrase_is_skipped(self):
        spec = _module('sample_test')
        # "没通过"本身已是 failed 的短语，必须命中 failed 而不是 passed
        self.assertEqual(catalog.state_match(spec, '没通过的有几条').key, 'failed')
        self.assertIsNone(catalog.state_match(spec, '还没合格的'),
                          '"合格"被"没"否定后，不能退而命中反义状态')
        self.assertEqual(catalog.state_match(spec, '不合格的有几条').key, 'failed',
                         '本身带否定的短语（不合格）不受影响')

    def test_negation_headed_phrase_matches(self):
        spec = _module('sample_test')
        self.assertEqual(catalog.state_match(spec, '不通过的有几条').key, 'failed')
        self.assertEqual(catalog.state_match(spec, '没通过的有几条').key, 'failed')

    def test_credit_disagreement(self):
        spec = _module('customer_credit')
        self.assertEqual(catalog.state_match(spec, '不同意的有几条').key, 'rejected')
        self.assertEqual(catalog.state_match(spec, '同意的有几条').key, 'agreed')
