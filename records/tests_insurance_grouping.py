"""保险只读聚合（阶段 1）测试。

覆盖归一化函数、标的/年度聚合、断档检测、续保报告，以及三个只读页面
的权限与渲染。全部使用现有 Insurance 模型，不新增表、不写业务数据。
"""

import json
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Permission, Role

from . import insurance_grouping as grouping
from .models import Insurance

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
        if hasattr(user, '_rbac_codes_cache'):
            del user._rbac_codes_cache
    cache.clear()
    return role


def rec(pk, title, company, premium, issue, end, order='NO1', department='行政部'):
    """构造未入库的 Insurance 实例，纯服务层测试用。"""
    return Insurance(
        id=pk,
        title=title,
        company_name=company,
        premium=Decimal(str(premium)),
        issue_date=issue,
        end_date=end,
        order_number=order,
        department=department,
    )


class InsuranceNormalizationTests(TestCase):
    def test_extract_plate(self):
        self.assertEqual(grouping.extract_plate('车险 粤B8X66D'), '粤B8X66D')
        self.assertEqual(grouping.extract_plate('粤BDE1559'), '粤BDE1559')
        self.assertEqual(grouping.extract_plate('财产一切险（宿舍）'), '')

    def test_normalize_insurer(self):
        self.assertEqual(
            grouping.normalize_insurer('中国太平洋财产保险股份有限公司深圳分公司'),
            '中国太平洋财产保险',
        )
        self.assertEqual(
            grouping.normalize_insurer('中国太平洋财产保险股份深圳分公司'),
            '中国太平洋财产保险',
        )
        self.assertEqual(
            grouping.normalize_insurer('中国平安财产保险股份有限公司'),
            '中国平安财产保险',
        )
        self.assertEqual(
            grouping.normalize_insurer('中国人民财产保险股份有限公司深圳市分公司'),
            '中国人民财产保险',
        )

    def test_normalize_insurance_type(self):
        self.assertEqual(grouping.normalize_insurance_type('车险 粤B8X66D'), '车险')
        self.assertEqual(grouping.normalize_insurance_type('产品责任险保单&投保单'), '产品责任险')
        self.assertEqual(grouping.normalize_insurance_type('产品责任保险'), '产品责任险')
        self.assertEqual(grouping.normalize_insurance_type('产品质量保证保险保单&投保单'), '产品质量保证险')
        self.assertEqual(
            grouping.normalize_insurance_type('A股上市公司、董监事及高管责任保险'),
            '董监事及高管责任险',
        )
        self.assertEqual(
            grouping.normalize_insurance_type('高新技术企业产品责任保险'),
            '产品责任险',
        )


class InsuranceGroupingTests(TestCase):
    today = date(2026, 10, 22)

    def test_car_chain_and_gap(self):
        records = [
            rec(1, '车险 粤B8X66D', '中国太平洋财产保险股份有限公司深圳分公司', 1000,
                date(2025, 7, 17), date(2026, 7, 16)),
            rec(2, '粤B8X66D', '中国平安财产保险股份有限公司', 1100,
                date(2026, 8, 26), date(2027, 8, 25)),
        ]
        subjects = grouping.build_subjects(records, today=self.today)
        self.assertEqual(len(subjects), 1)
        subject = subjects[0]
        self.assertEqual(subject.key, 'car:粤B8X66D')
        self.assertEqual(subject.term_count, 2)
        self.assertEqual(subject.status, 'active')
        self.assertTrue(subject.has_gap)
        self.assertEqual(subject.max_gap_days, 40)
        self.assertTrue(subject.insurer_changed)
        self.assertEqual(float(subject.yoy_pct), 10.0)
        self.assertEqual(subject.latest.year, 2027)

    def test_non_car_titles_merge_into_one_chain(self):
        records = [
            rec(1, '产品责任险保单&投保单', '中国平安财产保险股份有限公司深圳分公司', 45000,
                date(2024, 5, 21), date(2025, 5, 20)),
            rec(2, '产品责任险', '中国太平洋财产保险股份有限公司珠海中心支公司', 35000,
                date(2025, 5, 21), date(2026, 5, 20)),
            rec(3, '产品责任保险', '中国太平洋财产保险股份有限公司', 48000,
                date(2026, 5, 22), date(2027, 5, 21)),
        ]
        subjects = grouping.build_subjects(records, today=self.today)
        self.assertEqual(len(subjects), 1)
        subject = subjects[0]
        self.assertEqual(subject.key, 'noncar:产品责任险')
        self.assertEqual(subject.term_count, 3)
        self.assertEqual(subject.status, 'active')
        self.assertTrue(subject.has_gap)
        self.assertEqual(subject.max_gap_days, 1)
        self.assertAlmostEqual(float(subject.yoy_pct), 37.1, places=1)

    def test_same_year_items_are_one_term(self):
        records = [
            rec(1, '财产一切险（宿舍）', '中国太平洋财产保险股份有限公司', 264,
                date(2025, 11, 15), date(2026, 11, 14)),
            rec(2, '财产一切险（总部、工厂、分子公司、办事处）', '中国太平洋财产保险股份有限公司', 160960.11,
                date(2025, 11, 15), date(2026, 11, 14)),
        ]
        subjects = grouping.build_subjects(records, today=self.today)
        self.assertEqual(len(subjects), 1)
        subject = subjects[0]
        self.assertEqual(subject.key, 'noncar:财产一切险')
        self.assertEqual(subject.term_count, 1)
        self.assertEqual(subject.latest.record_count, 2)
        self.assertEqual(subject.latest.premium, Decimal('161224.11'))

    def test_report_detects_should_renew_gap_and_missing_order(self):
        records = [
            rec(1, '车险 粤B8X66D', '中国太平洋财产保险股份有限公司', 1000,
                date(2025, 7, 17), date(2026, 7, 16), order=''),
            rec(2, '粤B8X66D', '中国太平洋财产保险股份有限公司', 1100,
                date(2026, 8, 26), date(2027, 8, 25)),
            rec(3, '董监事及高级管理人员责任保险合同', '华泰财产保险有限公司', 325000,
                date(2025, 6, 16), date(2026, 6, 15)),
        ]
        subjects = grouping.build_subjects(records, today=self.today)
        report = grouping.build_report(subjects)
        self.assertEqual(report['should_renew_count'], 1)
        self.assertEqual(report['gap_count'], 1)
        self.assertEqual(report['missing_order_count'], 1)
        self.assertEqual(report['should_renew'][0].label, '董监事及高管责任险')


class InsuranceAggregationViewTests(TestCase):
    def setUp(self):
        call_command('sync_rbac', verbosity=0)
        self.user = User.objects.create_user('ins_user', 'ins@example.com', 'x12345678')
        grant('insurance', ('read',), self.user)
        today = timezone.localdate()
        Insurance.objects.create(
            company_name='中国太平洋财产保险股份有限公司深圳分公司',
            title='车险 粤B8X66D',
            order_number='A1',
            premium=Decimal('1000.00'),
            issue_date=today - timedelta(days=400),
            end_date=today - timedelta(days=35),
            department='行政部',
        )
        Insurance.objects.create(
            company_name='中国太平洋财产保险股份有限公司深圳分公司',
            title='粤B8X66D',
            order_number='A2',
            premium=Decimal('1100.00'),
            issue_date=today - timedelta(days=34),
            end_date=today + timedelta(days=330),
            department='行政部',
        )
        Insurance.objects.create(
            company_name='华泰财产保险有限公司',
            title='董监事及高级管理人员责任保险合同',
            order_number='',
            premium=Decimal('325000.00'),
            issue_date=today - timedelta(days=500),
            end_date=today - timedelta(days=100),
            department='内控部',
        )

    def test_subject_list_renders(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse('insurance_subject_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '保险续保视图')
        self.assertContains(resp, '粤B8X66D')
        self.assertContains(resp, '应续未续')

    def test_subject_detail_renders_timeline(self):
        self.client.force_login(self.user)
        resp = self.client.get(
            reverse('insurance_subject_detail'), {'key': 'car:粤B8X66D'}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '粤B8X66D 续保时间线')
        self.assertContains(resp, '续保时间线')
        self.assertContains(resp, '2027 年度')

    def test_renewal_report_renders(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse('insurance_renewal_report'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '保险续保报告')
        self.assertContains(resp, '应续未续')
        self.assertContains(resp, '董监事及高管责任险')

    def test_detail_modal_contains_timeline(self):
        self.client.force_login(self.user)
        insurance = Insurance.objects.filter(title='粤B8X66D').first()
        resp = self.client.get(
            reverse('insurance_detail', args=[insurance.pk]),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(resp.status_code, 200)
        html = json.loads(resp.content)['html']
        self.assertIn('续保时间线', html)
        self.assertIn('粤B8X66D', html)

    def test_requires_read_permission(self):
        no_role = User.objects.create_user('ins_norole', 'n@example.com', 'x12345678')
        self.client.force_login(no_role)
        resp = self.client.get(reverse('insurance_subject_list'))
        self.assertEqual(resp.status_code, 403)
