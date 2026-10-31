"""工具行为测试：查询、聚合、数据隔离、脱敏。"""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import Permission, Role
from copilot import registry
from copilot.models import CopilotConversation, CopilotPolicy, CopilotRun
from records.models import CustomerComplaint, CustomerLedger, DailyRecord

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
User = get_user_model()


def grant(user, *codes):
    role = Role.objects.create(code=f'role_{user.pk}_{len(codes)}', name='测试角色')
    for code in codes:
        permission = Permission.objects.filter(code=code).first()
        if permission:
            role.permissions.add(permission)
    role.users.add(user)
    return role


@override_settings(CACHES=LOCMEM)
class ToolTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('tool_admin', 'a@x.com', 'p')
        self.other = User.objects.create_user('tool_other', password='p')
        self.conversation = CopilotConversation.objects.create(user=self.admin)
        self.run = CopilotRun.objects.create(
            conversation=self.conversation, user=self.admin, question='测试')
        self.policy = CopilotPolicy()

        self.c1 = CustomerComplaint.objects.create(
            customer_name='华建建材', complaint_date=date(2026, 9, 10),
            complaint_status='处理中', compensation_amount=Decimal('1000.00'),
        )
        self.c2 = CustomerComplaint.objects.create(
            customer_name='华建建材', complaint_date=date(2026, 9, 20),
            complaint_status='已完成', compensation_amount=Decimal('500.00'),
        )
        self.c3 = CustomerComplaint.objects.create(
            customer_name='远大幕墙', complaint_date=date(2026, 8, 1),
            complaint_status='未完成', compensation_amount=Decimal('300.00'),
        )
        self.ledger = CustomerLedger.objects.create(
            customer_name='华建建材', legal_id_card='310101199001011234',
            credit_limit=Decimal('200'),
        )

    def ctx(self, user=None, is_local=True):
        return registry.ToolContext(
            user=user or self.admin, conversation=self.conversation,
            run=self.run, is_local=is_local, policy=self.policy,
        )

    def call(self, name, **args):
        return registry.run_tool(registry.get(name), self.ctx(), args)

    # ---------------------------------------------------------- 列表查询

    def test_query_returns_rows_and_labels(self):
        result = self.call('query_records', module='customer_complaint')
        self.assertTrue(result.ok)
        self.assertEqual(result.data['total'], 3)
        self.assertEqual(result.data['module_label'], '客户客诉')
        self.assertIn('customer_name', result.data['columns'])
        self.assertEqual(result.data['columns']['customer_name'], '客户名称')

    def test_query_keyword_filter(self):
        result = self.call('query_records', module='customer_complaint',
                           keyword='远大')
        self.assertEqual(result.data['total'], 1)
        self.assertEqual(result.data['rows'][0]['customer_name'], '远大幕墙')

    def test_query_date_range(self):
        result = self.call('query_records', module='customer_complaint',
                           date_from='2026-09-01', date_to='2026-09-30')
        self.assertEqual(result.data['total'], 2)

    def test_query_empty_result_hints_latest_date(self):
        """日期猜错时（如把"最近"当成 2023 年）要告诉模型最新数据在哪天。"""
        result = self.call('query_records', module='customer_complaint',
                           date_from='2023-01-01', date_to='2023-03-31')
        self.assertEqual(result.data['total'], 0)
        self.assertIn('最新一条的日期', result.data['hint'])

    def test_query_refs_have_urls(self):
        result = self.call('query_records', module='customer_complaint', limit=1)
        self.assertTrue(result.refs)
        self.assertTrue(result.refs[0]['url'].startswith('/'))

    def test_query_truncated_flag(self):
        result = self.call('query_records', module='customer_complaint', limit=1)
        self.assertTrue(result.truncated)
        self.assertEqual(result.row_count, 1)

    # ---------------------------------------------------------- 详情

    def test_get_record_ok(self):
        result = self.call('get_record', module='customer_complaint',
                           record_id=self.c1.pk)
        self.assertTrue(result.ok)
        self.assertEqual(result.data['record']['customer_name'], '华建建材')

    def test_get_record_not_found(self):
        result = self.call('get_record', module='customer_complaint',
                           record_id=999999)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, 'NOT_FOUND')

    def test_get_record_masks_id_card(self):
        result = self.call('get_record', module='customer_ledger',
                           record_id=self.ledger.pk)
        self.assertTrue(result.ok)
        value = result.data['record']['legal_id_card']
        self.assertNotEqual(value, '310101199001011234')
        self.assertIn('*', value)

    # ---------------------------------------------------------- 聚合

    def test_aggregate_count_group_by(self):
        result = self.call('aggregate_records', module='customer_complaint',
                           metric='count', group_by='customer_name')
        self.assertTrue(result.ok)
        rows = {row['客户名称']: row['value'] for row in result.data['rows']}
        self.assertEqual(rows['华建建材'], 2)
        self.assertEqual(rows['远大幕墙'], 1)

    def test_aggregate_sum(self):
        result = self.call('aggregate_records', module='customer_complaint',
                           metric='sum', field='compensation_amount')
        self.assertEqual(result.data['rows'][0]['value'], 1800.0)

    def test_aggregate_rejects_non_numeric_field(self):
        result = self.call('aggregate_records', module='customer_complaint',
                           metric='sum', field='customer_name')
        self.assertFalse(result.ok)
        self.assertEqual(result.error, 'BAD_ARGS')

    def test_aggregate_rejects_bad_group(self):
        result = self.call('aggregate_records', module='customer_complaint',
                           metric='count', group_by='remarks')
        self.assertFalse(result.ok)
        self.assertEqual(result.error, 'BAD_ARGS')

    # ---------------------------------------------------------- 数据隔离

    def test_daily_record_scope_is_own(self):
        DailyRecord.objects.create(
            user=self.other, record_date=timezone.now(),
            department='销售', work_content='别人的记录', status='完成',
        )
        DailyRecord.objects.create(
            user=self.admin, record_date=timezone.now(),
            department='销售', work_content='我的记录', status='完成',
        )
        result = self.call('query_records', module='daily_record')
        self.assertEqual(result.data['total'], 1)
        self.assertEqual(result.data['rows'][0]['work_content'], '我的记录')

    # ---------------------------------------------------------- 搜索与附件

    def test_global_search_hits(self):
        result = self.call('global_search', keyword='华建')
        self.assertTrue(result.ok)
        self.assertGreaterEqual(result.data['total'], 3)
        modules = {g['module'] for g in result.data['groups']}
        self.assertIn('customer_complaint', modules)

    def test_global_search_no_hit(self):
        result = self.call('global_search', keyword='不存在的公司xyz')
        self.assertTrue(result.ok)
        self.assertEqual(result.data['total'], 0)

    def test_global_search_splits_multi_terms(self):
        """「华建建材 客诉」这类组合词不能整串 icontains，否则必然 0 条。"""
        result = self.call('global_search', keyword='华建建材 客诉')
        self.assertTrue(result.ok)
        self.assertGreaterEqual(result.data['total'], 1)
        self.assertEqual(result.data['terms'], ['华建建材', '客诉'])

    def test_global_search_falls_back_to_first_term(self):
        """组合词里有一个词在任何字段都找不到时，退一步只按首个词搜。"""
        result = self.call('global_search', keyword='华建建材 付款凭证')
        self.assertTrue(result.ok)
        self.assertGreaterEqual(result.data['total'], 1)

    def test_list_attachments_empty(self):
        result = self.call('list_attachments', module='customer_complaint',
                           record_id=self.c1.pk)
        self.assertTrue(result.ok)
        self.assertEqual(result.data['total'], 0)

    def test_list_attachments_module_not_registered(self):
        result = self.call('list_attachments', module='customer_ledger',
                           record_id=self.ledger.pk)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, 'NO_ATTACHMENTS')

    # ---------------------------------------------------------- 细粒度权限

    def test_partial_permission_filters_modules(self):
        viewer = User.objects.create_user('tool_viewer', password='p')
        grant(viewer, 'customer_complaint.read', 'copilot.read')
        keys = [m.key for m in registry.available_modules(viewer)]
        self.assertEqual(keys, ['customer_complaint'])

        denied = registry.run_tool(
            registry.get('query_records'),
            registry.ToolContext(user=viewer, conversation=self.conversation,
                                 run=self.run, is_local=True, policy=self.policy),
            {'module': 'customer_ledger'})
        self.assertFalse(denied.ok)
        self.assertEqual(denied.error, 'PERM_DENIED')
