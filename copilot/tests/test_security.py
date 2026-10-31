"""安全层测试：脱敏、分级外发、链接消毒。"""

from django.test import TestCase, override_settings

from copilot import security
from copilot.models import CopilotPolicy

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


class MaskTests(TestCase):
    def test_id_card(self):
        self.assertEqual(security.mask_value('legal_id_card', '310101199001011234'),
                         '310101********1234')

    def test_phone_in_free_text(self):
        self.assertEqual(security.mask_text('联系 13812345678 处理'),
                         '联系 138****5678 处理')

    def test_email(self):
        self.assertEqual(security.mask_value('email', 'zhang@corp.com'), 'z***@corp.com')

    def test_policy_number_keeps_tail(self):
        masked = security.mask_value('order_number', 'P202610310001')
        self.assertTrue(masked.endswith('0001'))
        self.assertIn('*', masked)

    def test_short_value_not_leaked(self):
        self.assertEqual(security.mask_value('phone', '123'), '***')

    def test_plain_value_untouched(self):
        self.assertEqual(security.mask_value('customer_name', '华建建材'), '华建建材')

    def test_mask_row_reports_fields(self):
        from copilot import catalog
        spec = catalog.get_module('customer_ledger')
        row, masked = security.mask_row(spec, {
            'customer_name': '华建建材',
            'legal_id_card': '310101199001011234',
        })
        self.assertEqual(row['customer_name'], '华建建材')
        self.assertIn('legal_id_card', masked)


class EgressTests(TestCase):
    def setUp(self):
        CopilotPolicy.objects.all().delete()

    def test_l3_blocked_for_cloud_by_default(self):
        self.assertFalse(security.cloud_allowed('insurance'))
        self.assertFalse(security.cloud_allowed('customer_complaint'))

    def test_l1_l2_allowed_for_cloud(self):
        self.assertTrue(security.cloud_allowed('regulation'))
        self.assertTrue(security.cloud_allowed('decoration'))

    def test_policy_can_open_l3(self):
        CopilotPolicy.objects.create(allow_cloud_modules=['insurance'])
        self.assertTrue(security.cloud_allowed('insurance'))

    def test_local_always_allowed(self):
        from copilot import catalog
        spec = catalog.get_module('insurance')
        self.assertTrue(security.spec_allowed_for_egress(spec, is_local=True))

    def test_unknown_module_blocked(self):
        self.assertFalse(security.cloud_allowed('system'))


class SanitizeTests(TestCase):
    def test_fake_link_removed(self):
        text = '见 [客诉记录](/records/customer_complaint/detail/99/)'
        cleaned = security.sanitize_answer(text, [])
        self.assertNotIn('/records/customer_complaint/detail/99/', cleaned)
        self.assertIn('客诉记录', cleaned)

    def test_allowed_link_kept(self):
        url = '/records/customer_detail/1/'
        cleaned = security.sanitize_answer(f'[记录]({url})', [url])
        self.assertIn(url, cleaned)


@override_settings(CACHES=LOCMEM)
class RateLimitTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from django.core.cache import cache
        cache.clear()
        self.user = get_user_model().objects.create_user('rl_user', password='x')

    def test_limit_triggers(self):
        policy = CopilotPolicy(rate_limit_per_10min=2, rate_limit_per_day=100)
        self.assertTrue(security.check_rate_limit(self.user, policy).allowed)
        self.assertTrue(security.check_rate_limit(self.user, policy).allowed)
        verdict = security.check_rate_limit(self.user, policy)
        self.assertFalse(verdict.allowed)
        self.assertGreater(verdict.retry_after, 0)

    def test_zero_limit_means_unlimited(self):
        policy = CopilotPolicy(rate_limit_per_10min=0, rate_limit_per_day=0)
        for _ in range(5):
            self.assertTrue(security.check_rate_limit(self.user, policy).allowed)
