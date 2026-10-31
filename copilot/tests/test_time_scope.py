"""显式时间口径的确定性统计。

背景：``stat_query``/``sum_query``/``rank_query`` 此前对带时间词的问句一刀切
交回模型——因为"今年/本月"各模块日期字段语义不同，猜错就会把全表数字冒充
口径结果。但这把"2026年3月的客诉有多少"这类**能算出具体起止日**的问法也
挡在了本地链路外面。

本轮把 ``tools.daterange`` 接进三个统计意图：解析得出具体区间就本地算，解析
不出（相对口径）仍交模型。测试盯三件事：

1. 数字必须与直接按日期过滤的 SQL 统计一致；
2. 口径说明（时间范围）必须出现在答案里，读者能核对；
3. "今年/上月"这类模糊口径**必须**仍然交模型——这是本能力的边界，不是缺陷。
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

    def test_vague_scope_still_goes_to_model(self):
        """相对口径解析不出区间：必须交模型，这是边界不是缺陷。"""
        for question in ('今年的客诉有多少条', '上个月的客诉有多少条',
                         '本月客诉有多少条'):
            self.assertIsNone(planner.stat_query(question, self.modules,
                                                 self.admin), question)


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
        self.assertIsNone(planner.sum_query('今年客诉赔偿金额一共多少钱',
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
        self.assertIsNone(planner.rank_query('本月客诉中赔偿金额最大的是哪个',
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
