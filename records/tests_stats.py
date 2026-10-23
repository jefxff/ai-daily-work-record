"""统计口径测试（复核报告 DATA-04）。

关键点：核减金额从 Python 全表遍历改为 SQL 聚合后，必须与原实现逐条等价；
看板统计必须与首页共用 records/stats.py，模板 context 键保持不变。
"""

from decimal import Decimal

from datetime import date

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from accounts.models import Permission, Role

from . import stats
from .models import Decoration, PriceEvaluation

User = get_user_model()


def grant(module, actions=('read',), user=None):
    role, _ = Role.objects.get_or_create(
        code=f't_{module}', defaults={'name': f'测试-{module}', 'is_active': True}
    )
    for action in actions:
        perm, _ = Permission.objects.get_or_create(
            module=module,
            action=action,
            defaults={'code': f'{module}:{action}', 'name': f'{module}-{action}'},
        )
        role.permissions.add(perm)
    if user is not None:
        role.users.add(user)
    cache.clear()
    return role


def make_price(s1=None, s2=None, s3=None, internal=None):
    return PriceEvaluation.objects.create(
        project_category='五金', proposing_department='采购部', proposer='张三',
        supplier1_price=s1, supplier2_price=s2, supplier3_price=s3,
        internal_control_price=Decimal(str(internal)),
    )


class PriceStatsTests(TestCase):
    def test_sql_reduction_matches_python_reference(self):
        make_price(100, 200, None, 150)      # 200-150=50
        make_price(None, None, 90, 100)      # 90-100=-10（可为负）
        make_price(0, 0, 0, 120)             # 0-120=-120（报价全 0 也算有效报价）
        make_price(500, 400, 300, 0)         # 内控价为 0：原实现不计
        expected = sum(
            obj.get_reduction_amount() or 0 for obj in PriceEvaluation.objects.all()
        )
        self.assertEqual(stats.price_stats()['total_price_reduction'], expected)

    def test_empty_table_returns_zero(self):
        result = stats.price_stats()
        self.assertEqual(result['total_price_evaluations'], 0)
        self.assertEqual(result['total_price_reduction'], Decimal('0'))

    def test_reduction_is_pushed_down_to_sql(self):
        """COUNT + SUM 两条查询完成，不再按行取回 Python。"""
        make_price(100, 200, 300, 150)
        with self.assertNumQueries(2):
            stats.price_stats()


class DashboardSummaryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('stats_viewer', password='Pwd#123456')
        grant('data_view', user=self.user)
        grant('daily_record', ('read', 'create', 'update'), user=self.user)
        grant('decoration', ('read',), user=self.user)
        self.client.force_login(self.user)

    def test_summary_keys_and_totals(self):
        Decoration.objects.create(
            project_name='车间改造', constructor='某施工队', start_date=date(2026, 1, 5),
            original_price=Decimal('1000'), final_price=Decimal('800'),
            reduced_price=Decimal('200'),
        )
        summary = stats.dashboard_summary(
            self.user, {'data_view', 'daily_record', 'decoration'},
            insurance_total=3, insurance_active=2, insurance_expired=1,
        )
        self.assertEqual(summary['total_decorations'], 1)
        self.assertEqual(summary['decoration_reduced_total'], Decimal('200'))
        self.assertEqual(summary['insurance_total_count'], 3)
        self.assertEqual(summary['insurance_expired_count'], 1)
        self.assertGreaterEqual(summary['total_records'], 1)

    def test_dashboard_page_renders_with_same_context(self):
        resp = self.client.get(reverse('dashboard'))
        self.assertEqual(resp.status_code, 200)
        for key in ('total_records', 'module_count', 'completion_rate',
                    'pending_items', 'decoration_reduced_total',
                    'insurance_total_count', 'chart_payload', 'module_badges'):
            self.assertIn(key, resp.context)
