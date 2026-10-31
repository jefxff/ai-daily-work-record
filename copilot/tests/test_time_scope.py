"""时间口径的确定性统计。

背景：``stat_query``/``sum_query``/``rank_query`` 此前对带时间词的问句一刀切
交回模型——因为"今年/本月"各模块日期字段语义不同，猜错就会把全表数字冒充
口径结果。但这把"2026年3月的客诉有多少"这类**能算出具体起止日**的问法也
挡在了本地链路外面。

现在分两档，都以"能不能换算成确定的起止日"为准：

1. 问句里写了日期（2026年3月 / 最近30天 / 2026 Q3）→ 本地算；
2. 日历上无歧义的相对口径（上个月 / 本月 / 今年 / 上周）→ 本地算。

算不出来的（"最近""近期"这种没有刻度的）仍交模型——这是能力边界，不是缺陷。
测试盯三件事：数字必须与 SQL 一致、口径说明必须出现在答案里、边界的仍交模型。
"""

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from copilot import registry
from copilot.engine import planner
from copilot.models import CopilotConversation, CopilotPolicy, CopilotRun
from records.models import CustomerComplaint

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
User = get_user_model()


@override_settings(CACHES=LOCMEM)
class TimeScopeBase(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_superuser('ts_admin', 'ts@x.com', 'p')
        CopilotPolicy.objects.all().delete()
        CopilotPolicy.objects.create(rate_limit_per_10min=100,
                                     rate_limit_per_day=1000, max_tool_rounds=3)
        self.policy = CopilotPolicy.current()
        self.modules = registry.available_modules(self.admin, True, self.policy)
        self.conversation = CopilotConversation.objects.create(user=self.admin)
        self.run = CopilotRun.objects.create(
            conversation=self.conversation, user=self.admin, question='q')

    def complaint(self, name, day, amount='100.00'):
        return CustomerComplaint.objects.create(
            customer_name=name, complaint_date=day,
            complaint_status='处理中', compensation_amount=Decimal(amount))

    @staticmethod
    def last_month_day(day=15):
        """上个月里一个稳定的日期（不随今天靠近月初而落到本月）。"""
        first = timezone.localdate().replace(day=1)
        prev_end = first - timedelta(days=1)
        return prev_end.replace(day=min(day, prev_end.day))

    @staticmethod
    def this_month_day():
        return timezone.localdate().replace(day=1)


class CountTests(TimeScopeBase):
    def test_explicit_month_counted(self):
        """「2026年3月的客诉有多少条」→ 只数 3 月，不把全表当口径。"""
        self.complaint('甲', date(2026, 3, 5))
        self.complaint('乙', date(2026, 3, 20))
        self.complaint('丙', date(2026, 4, 1))       # 4 月，不计入
        self.complaint('丁', date(2025, 3, 5))       # 去年 3 月，不计入
        intent = planner.stat_query('2026年3月的客诉有多少条',
                                    self.modules, self.admin)
        self.assertIsNotNone(intent, '显式月份应走本地统计')
        self.assertEqual(intent['spec'].key, 'customer_complaint')
        answer, _refs = planner.stat_answer(intent, self.admin)
        self.assertIn('2 条', answer.replace('**', ''))
        self.assertIn('2026年3月', answer)

    def test_quarter_counted(self):
        self.complaint('甲', date(2026, 1, 15))
        self.complaint('乙', date(2026, 2, 1))
        self.complaint('丙', date(2026, 4, 1))       # 二季度，不计入
        intent = planner.stat_query('2026年一季度的客诉有多少条',
                                    self.modules, self.admin)
        self.assertIsNotNone(intent)
        answer, _refs = planner.stat_answer(intent, self.admin)
        self.assertIn('2 条', answer.replace('**', ''))
        self.assertIn('2026年第1季度', answer)

    def test_year_range_counted(self):
        """「2025到2026年」是跨年区间，不是单年。"""
        self.complaint('甲', date(2025, 6, 1))
        self.complaint('乙', date(2026, 6, 1))
        self.complaint('丙', date(2024, 6, 1))       # 区间外
        intent = planner.stat_query('2025到2026年的客诉有多少条',
                                    self.modules, self.admin)
        self.assertIsNotNone(intent)
        answer, _refs = planner.stat_answer(intent, self.admin)
        self.assertIn('2 条', answer.replace('**', ''))
        self.assertIn('2025', answer)

    def test_unparsable_scope_still_goes_to_model(self):
        """没有刻度的"最近/近期"换算不出区间：仍交模型，这是边界不是缺陷。"""
        for question in ('最近的客诉有多少条', '近期客诉有多少条',
                         '最近这批客诉有多少条'):
            self.assertIsNone(planner.stat_query(question, self.modules,
                                                 self.admin), question)


class RelativeScopeTests(TimeScopeBase):
    """相对口径日历上无歧义，本地换算。

    以前一律交模型：模型没配就降级成"没有记录"（«上个月客诉有多少»真值 12 条），
    模型配了也只是把口径交给它猜。既然"上个月是哪天到哪天"是确定的，就该算。
    """

    def test_last_month_counted(self):
        last = self.last_month_day()
        self.complaint('甲', last)
        self.complaint('乙', last)
        self.complaint('丙', self.this_month_day())    # 本月，不计入
        intent = planner.stat_query('上个月客诉有多少条', self.modules, self.admin)
        self.assertIsNotNone(intent, '相对口径应走本地统计')
        answer, _refs = planner.stat_answer(intent, self.admin)
        self.assertIn('2 条', answer.replace('**', ''))
        self.assertIn('上个月', answer)
        # 口径写的是具体年月，读者能核对
        self.assertIn(str(last.year), answer)

    def test_this_year_counted(self):
        today = timezone.localdate()
        self.complaint('甲', date(today.year, 1, 5))
        self.complaint('乙', date(today.year - 1, 6, 1))   # 去年，不计入
        intent = planner.stat_query('今年的客诉有多少条', self.modules, self.admin)
        self.assertIsNotNone(intent)
        answer, _refs = planner.stat_answer(intent, self.admin)
        self.assertIn('1 条', answer.replace('**', ''))
        self.assertIn(f'{today.year}年', answer)

    def test_last_month_sum(self):
        last = self.last_month_day(5)
        self.complaint('甲', last, '100.50')
        self.complaint('乙', last, '200.50')
        self.complaint('丙', self.this_month_day(), '999.00')   # 区间外
        intent = planner.sum_query('上个月赔偿金额合计多少', self.modules, self.admin)
        self.assertIsNotNone(intent, '相对口径的求和应走本地')
        answer, _refs = planner.sum_answer(intent, self.admin)
        plain = answer.replace(',', '').replace('**', '')
        self.assertIn('301', plain)
        self.assertIn('上个月', answer)

    def test_last_month_rank(self):
        last = self.last_month_day(6)
        self.complaint('甲', last, '300.00')
        self.complaint('乙', last, '500.00')
        self.complaint('丙', self.this_month_day(), '900.00')   # 金额更大但区间外
        intent = planner.rank_query('上个月客诉中赔偿金额最大的是哪个',
                                    self.modules, self.admin)
        self.assertIsNotNone(intent, '相对口径的极值应走本地排序')
        _answer, refs = planner.rank_answer(intent, self.admin)
        self.assertTrue(refs)
        top = CustomerComplaint.objects.get(pk=refs[0]['id'])
        self.assertEqual(top.customer_name, '乙')


class SumTests(TimeScopeBase):
    def test_explicit_month_sum(self):
        """「2026年3月赔偿金额一共多少钱」→ 只加 3 月的，金额与 SQL 一致。"""
        self.complaint('甲', date(2026, 3, 5), '100.50')
        self.complaint('乙', date(2026, 3, 9), '200.50')
        self.complaint('丙', date(2026, 5, 1), '999.00')   # 区间外
        intent = planner.sum_query('2026年3月客诉赔偿金额一共多少钱',
                                   self.modules, self.admin)
        self.assertIsNotNone(intent)
        answer, _refs = planner.sum_answer(intent, self.admin)
        plain = answer.replace(',', '').replace('**', '')
        self.assertIn('301', plain)
        self.assertNotIn('1,300', answer)
        self.assertIn('2026年3月', answer)

    def test_vague_sum_still_goes_to_model(self):
        """没有刻度的"最近"换算不出区间，仍交模型。"""
        self.assertIsNone(planner.sum_query('最近客诉赔偿金额一共多少钱',
                                            self.modules, self.admin))


class RankTests(TimeScopeBase):
    def test_explicit_month_rank(self):
        """区间内的极值：4 月的更高也不该出现，冠军必须来自 3 月。"""
        self.complaint('甲', date(2026, 3, 5), '300.00')
        self.complaint('乙', date(2026, 3, 9), '500.00')
        self.complaint('丙', date(2026, 4, 1), '900.00')   # 金额更大但区间外
        intent = planner.rank_query('2026年3月客诉中赔偿金额最大的是哪个',
                                    self.modules, self.admin)
        self.assertIsNotNone(intent, '显式月份的极值应走本地排序')
        answer, refs = planner.rank_answer(intent, self.admin)
        self.assertTrue(refs)
        top = CustomerComplaint.objects.get(pk=refs[0]['id'])
        self.assertEqual(top.customer_name, '乙')
        self.assertIn('2026年3月', answer)

    def test_vague_rank_still_goes_to_model(self):
        """没有刻度的"最近"换算不出区间，仍交模型。"""
        self.assertIsNone(planner.rank_query('最近客诉中赔偿金额最大的是哪个',
                                             self.modules, self.admin))


class RollingWindowTests(TimeScopeBase):
    def test_recent_days_window(self):
        """「最近7天」按今天回溯：窗口内算，窗口外不算。"""
        today = timezone.localdate()
        self.complaint('甲', today)
        self.complaint('乙', today - timedelta(days=3))
        self.complaint('丙', today - timedelta(days=30))
        intent = planner.stat_query('最近7天的客诉有多少条',
                                    self.modules, self.admin)
        self.assertIsNotNone(intent)
        answer, _refs = planner.stat_answer(intent, self.admin)
        self.assertIn('2', answer.replace('**', ''))
