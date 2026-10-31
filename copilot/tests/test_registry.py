"""工具注册表测试：权限边界、参数校验、失败留痕。"""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from copilot import registry
from copilot.models import (CopilotConversation, CopilotPolicy, CopilotRun,
                            CopilotToolCall)

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
User = get_user_model()


@override_settings(CACHES=LOCMEM)
class RegistryTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('reg_admin', 'a@x.com', 'p')
        self.nobody = User.objects.create_user('reg_nobody', password='p')
        self.conversation = CopilotConversation.objects.create(user=self.admin)
        self.run = CopilotRun.objects.create(
            conversation=self.conversation, user=self.admin, question='测试问题')
        self.policy = CopilotPolicy()

    def ctx(self, user=None, is_local=True):
        return registry.ToolContext(
            user=user or self.admin, conversation=self.conversation,
            run=self.run, is_local=is_local, policy=self.policy,
        )

    def test_tools_registered(self):
        expected = {'list_modules', 'schema_of_module', 'global_search',
                    'query_records', 'get_record', 'aggregate_records',
                    'list_attachments', 'search_knowledge'}
        self.assertEqual(set(registry.TOOLS), expected)

    def test_admin_sees_all_modules(self):
        keys = [m.key for m in registry.available_modules(self.admin)]
        self.assertIn('customer_complaint', keys)
        self.assertIn('metal_price', keys)
        self.assertNotIn('system', keys)

    def test_user_without_permission_sees_nothing(self):
        self.assertEqual(registry.available_modules(self.nobody), [])

    def test_specs_exclude_module_tools_without_permission(self):
        names = {s.name for s in registry.available_specs(self.nobody)}
        self.assertIn('list_modules', names)
        self.assertNotIn('query_records', names)

    def test_permission_denied_is_logged(self):
        spec = registry.get('query_records')
        result = registry.run_tool(spec, self.ctx(user=self.nobody),
                                   {'module': 'customer_complaint'})
        self.assertFalse(result.ok)
        self.assertEqual(result.error, 'PERM_DENIED')
        call = CopilotToolCall.objects.filter(tool_name='query_records').latest('id')
        self.assertFalse(call.permitted)
        self.assertIn('无权', call.denied_reason)

    def test_cloud_egress_denied_for_l3(self):
        spec = registry.get('query_records')
        result = registry.run_tool(spec, self.ctx(is_local=False),
                                   {'module': 'insurance'})
        self.assertFalse(result.ok)
        self.assertEqual(result.error, 'EGRESS_DENIED')

    def test_cloud_egress_allowed_for_l1(self):
        spec = registry.get('query_records')
        result = registry.run_tool(spec, self.ctx(is_local=False),
                                   {'module': 'regulation'})
        self.assertTrue(result.ok)

    def test_unknown_module_rejected(self):
        spec = registry.get('query_records')
        result = registry.run_tool(spec, self.ctx(), {'module': 'system'})
        self.assertFalse(result.ok)
        self.assertIn(result.error, ('UNKNOWN_MODULE', 'PERM_DENIED'))

    def test_limit_is_clamped(self):
        spec = registry.get('query_records')
        result = registry.run_tool(spec, self.ctx(),
                                   {'module': 'customer_complaint', 'limit': 999})
        self.assertTrue(result.ok)

    def test_missing_required_param(self):
        spec = registry.get('query_records')
        result = registry.run_tool(spec, self.ctx(), {'keyword': 'x'})
        self.assertFalse(result.ok)
        self.assertEqual(result.error, 'BAD_ARGS')

    def test_bad_order_field(self):
        spec = registry.get('query_records')
        result = registry.run_tool(
            spec, self.ctx(),
            {'module': 'customer_complaint', 'order_by': 'remarks'})
        self.assertFalse(result.ok)
        self.assertEqual(result.error, 'BAD_ARGS')

    def test_bad_date_format(self):
        spec = registry.get('query_records')
        result = registry.run_tool(
            spec, self.ctx(),
            {'module': 'customer_complaint', 'date_from': '2026/09/01'})
        self.assertFalse(result.ok)
        self.assertEqual(result.error, 'BAD_ARGS')

    def test_unknown_tool_name(self):
        self.assertIsNone(registry.get('drop_table'))
