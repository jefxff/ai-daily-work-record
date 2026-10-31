"""字段字典的完整性与"到期/条件"口径。

这些用例守的是三类线上事故，都与"字典少一条声明"有关：

1. **漏字段**：模块新增了业务字段，字典没跟上，模型只能猜字段名；
2. **漏状态**：用户说"快到期"，字典里没有这个概念，条件被静默丢掉——
   问"保险中快到期有几条"答"共 20 份"，数字全真、结论全错；
3. **口径不一致**：字典写死 30 天，工作台预警是 60 天，同一个问题两个答案。

另一条硬要求：模块确实没有状态时，必须给出**可执行的下一步**，而不是一句
"该模块不支持"——模型看不懂就会原样重试两次，最后答"没有记录"。
"""

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from copilot import catalog, registry
from copilot.engine import planner
from copilot.tools import records as records_tool
from records.models import Insurance

User = get_user_model()
LOCMEM = {'default': {'django.core.cache.backends.locmem.LocMemCache'}}


class FieldDictionaryTests(TestCase):
    """字典与真实模型必须逐字段对得上。"""

    def test_declared_fields_exist(self):
        """声明了模型上不存在的字段 = 运行时 500 或静默空列。"""
        problems = {}
        for spec in catalog.MODULES:
            missing, _uncovered = catalog.field_audit(spec)
            if missing:
                problems[spec.key] = missing
        self.assertEqual(problems, {}, f'这些字段在模型上不存在：{problems}')

    def test_business_fields_all_declared(self):
        """除技术/外键/文件字段外，业务字段一个都不能漏。"""
        problems = {}
        for spec in catalog.MODULES:
            _missing, uncovered = catalog.field_audit(spec)
            if uncovered:
                problems[spec.key] = uncovered
        self.assertEqual(problems, {}, f'这些业务字段没进字典：{problems}')

    def test_state_keys_unique_per_module(self):
        for spec in catalog.MODULES:
            keys = [s.key for s in spec.states]
            self.assertEqual(len(keys), len(set(keys)), f'{spec.key} 状态 key 重复：{keys}')

    def test_state_labels_unique_per_module(self):
        for spec in catalog.MODULES:
            labels = [s.label for s in spec.states]
            self.assertEqual(len(labels), len(set(labels)),
                             f'{spec.key} 状态名重复：{labels}')

    def test_modules_without_states_explain_why(self):
        """没有状态的模块必须写明原因，否则字典校验分不清"故意"与"漏了"。"""
        for spec in catalog.MODULES:
            if not spec.states:
                self.assertTrue(spec.no_state_reason,
                                f'{spec.key} 没有 states，也没写 no_state_reason')

    def test_every_state_is_executable(self):
        """每个状态都必须能落到 SQL 上：写错字段名在这里就该炸。"""
        today = timezone.localdate()
        for spec in catalog.MODULES:
            model = catalog.model_of(spec)
            for state in spec.states:
                with self.subTest(module=spec.key, state=state.key):
                    state.apply_to(model.objects.all(), today).count()


class ExpiryWindowTests(TestCase):
    """字典里的窗口天数必须与工作台预警同源。"""

    def test_windows_match_expiry_meta(self):
        from records.expiry import SOURCE_META

        pairs = {
            'insurance': ('insurance', catalog.INSURANCE_LEAD_DAYS),
            'archive': ('archive_contract', catalog.CONTRACT_LEAD_DAYS),
            'license': ('license_expire', catalog.LICENSE_LEAD_DAYS),
            'license_review': ('license_review', catalog.REVIEW_LEAD_DAYS),
            'archive_borrow': ('borrow_due', catalog.BORROW_LEAD_DAYS),
            'license_borrow': ('borrow_due', catalog.BORROW_LEAD_DAYS),
        }
        for module_key, (kind, got) in pairs.items():
            self.assertEqual(got, SOURCE_META[kind]['lead_days'],
                             f'{module_key} 的提前天数与工作台不一致')

    def test_archive_expiring_uses_60_days(self):
        """曾经写死 30 天，导致"合同快到期"两个入口两个答案。"""
        spec = catalog.get_module('archive')
        state = catalog.get_state(spec, 'expiring')
        self.assertEqual(state.days, catalog.CONTRACT_LEAD_DAYS)

    def test_expiry_cue_builds_window_when_state_missing(self):
        """字典没有"即将到期"时，"快到期"要按 lead_days 换算成日期窗口。"""
        spec = catalog.get_module('insurance')
        state = planner._expiry_state(spec, '保险中快到期')
        self.assertIsNotNone(state)
        self.assertEqual(state.days, catalog.INSURANCE_LEAD_DAYS)

    def test_expiry_cue_ignored_without_lead_days(self):
        """没有到期概念的模块不该凭空造一个窗口。"""
        spec = catalog.get_module('regulation')
        self.assertIsNone(planner._expiry_state(spec, '制度快到期'))


class InsuranceSubjectStateTests(TestCase):
    """保险到期必须按"标的最新保单年度"判，不能逐条保单判。"""

    def setUp(self):
        self.today = timezone.localdate()
        self.spec = catalog.get_module('insurance')

    def make(self, title, end_date, issue_date=None):
        return Insurance.objects.create(
            company_name='测试保司', title=title,
            premium=Decimal('1000.00'),
            issue_date=issue_date or (end_date - timedelta(days=365)),
            end_date=end_date, department='测试',
        )

    def rows(self, key):
        state = catalog.get_state(self.spec, key)
        return state.apply_to(Insurance.objects.all(), self.today)

    def test_expiring_counts_latest_year_only(self):
        """同一标的续保后，旧年度不能再算一次到期。"""
        self.make('产品责任险（第 1 期）', self.today - timedelta(days=400))
        self.make('产品责任险（第 2 期）', self.today + timedelta(days=10))
        self.assertEqual(self.rows('expiring').count(), 1)

    def test_expired_excludes_renewed_subject(self):
        """老年度虽然过期，但标的已续到未来——不该报"应续未续"。"""
        self.make('货物运输险（第 1 期）', self.today - timedelta(days=400))
        self.make('货物运输险（第 2 期）', self.today + timedelta(days=300))
        self.assertEqual(self.rows('expired').count(), 0)

    def test_expired_respects_lookback(self):
        """超过 90 天的历史断保不返回，避免噪声。"""
        self.make('机器损坏险（第 1 期）', self.today - timedelta(days=200))
        self.assertEqual(self.rows('expired').count(), 0)

    def test_no_subject_state(self):
        self.make('未挂标的的保单', self.today + timedelta(days=10))
        self.assertEqual(self.rows('no_subject').count(), 1)


class ExpiryQuestionTests(TestCase):
    """端到端的问法：「保险中快到期有几条」不能再答全表。"""

    def setUp(self):
        self.admin = User.objects.create_superuser('dict_admin', 'd@x.com', 'p')
        self.modules = catalog.MODULES
        today = timezone.localdate()
        for title, end in (
            ('产品责任险（第 1 期）', today + timedelta(days=10)),
            ('货物运输险（第 1 期）', today + timedelta(days=200)),
            ('机器损坏险（第 1 期）', today - timedelta(days=5)),
        ):
            Insurance.objects.create(
                company_name='测试保司', title=title, premium=Decimal('100'),
                issue_date=end - timedelta(days=365), end_date=end, department='测试')

    def test_stat_query_applies_expiry_filter(self):
        intent = planner.stat_query('保险中快到期有几条', self.modules, self.admin)
        self.assertIsNotNone(intent, '"几条"应走本地统计')
        self.assertEqual(intent['spec'].key, 'insurance')
        self.assertIsNotNone(intent['state'], '"快到期"不能被丢掉')
        answer, _refs = planner.stat_answer(intent, self.admin)
        self.assertIn('1 份', answer.replace('**', ''))
        self.assertNotIn('3 份', answer.replace('**', ''))

    def test_list_question_uses_state(self):
        """「哪些快到期」要走列表分支，并且带上状态。"""
        spec, _keyword, state = planner.module_query(
            '保险中哪些快到期', self.modules, self.admin)
        self.assertEqual(getattr(spec, 'key', None), 'insurance')
        self.assertIsNotNone(state)

    def test_seeded_state_used_before_fallback(self):
        """字典里声明过的状态优先于通用窗口（保险要按标的归组）。"""
        intent = planner.stat_query('保险中已过期的有几份', self.modules, self.admin)
        self.assertEqual(intent['state'].key, 'expired')
        self.assertIsNotNone(intent['state'].apply)


class UnsupportedConditionTests(TestCase):
    """条件筛不了时，明说筛不了，不许数全表。"""

    def setUp(self):
        self.admin = User.objects.create_superuser('cond_admin', 'c@x.com', 'p')
        self.modules = catalog.MODULES

    def test_unknown_condition_returns_guidance(self):
        text = planner.unsupported_condition('已完成的装修有几项', self.modules, self.admin)
        self.assertTrue(text)
        self.assertIn('装修', text)
        self.assertIn('日期字段', text)

    def test_known_condition_is_not_blocked(self):
        for question in ('客诉已闭环的有几条', '保险中快到期有几条',
                         '客户账期中 同意的有多少条'):
            with self.subTest(question=question):
                self.assertEqual(
                    planner.unsupported_condition(question, self.modules, self.admin), '')

    def test_stat_query_refuses_unfiltered_count(self):
        """带条件词却没有任何筛选落地时，宁可不答，也不报全表。"""
        self.assertIsNone(
            planner.stat_query('已完成的装修有几条', self.modules, self.admin))

    def test_tool_denial_gives_next_step(self):
        """对没有状态的模块传 state，要告诉模型改用 date_from/date_to。"""
        spec = catalog.get_module('decoration')
        _state, error = records_tool._state_of(spec, '已过期')
        self.assertIsNotNone(error)
        self.assertIn('date_from', error.message)
        self.assertIn(spec.date_field, error.message)

    def test_tool_denial_lists_available_states(self):
        spec = catalog.get_module('insurance')
        _state, error = records_tool._state_of(spec, '不存在的状态')
        self.assertIsNotNone(error)
        for label in catalog.state_options(spec):
            self.assertIn(label, error.message)


class SchemaExposureTests(TestCase):
    """schema 是模型"唯一的字段来源"，口径必须显式给出。"""

    def test_schema_reports_lead_days(self):
        data = catalog.schema(catalog.get_module('insurance'))
        self.assertEqual(data['lead_days'], catalog.INSURANCE_LEAD_DAYS)
        self.assertIn('expiring_hint', data)

    def test_schema_reports_no_states_reason(self):
        data = catalog.schema(catalog.get_module('decoration'))
        self.assertIn('no_states', data)
        self.assertTrue(data['no_states'])

    def test_schema_state_notes(self):
        data = catalog.schema(catalog.get_module('insurance'))
        self.assertIn('即将到期', data.get('state_notes', {}))
