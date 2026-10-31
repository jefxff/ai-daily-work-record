"""全模块覆盖：每个业务模块的常见问法都要认得出、答得准。

2026-10-31 用户截图（本地模型，徽章「本地模型」）：

1. 「已借出的档案有哪些」→「目前没有查询到已借出的档案信息」
2. 「证照管理中 已过期的有哪些」→「证照管理中没有找到已过期的记录」

两句都是错的：系统里有 50 份档案（8 份借出中）、50 份证照（29 份已过期）。
根因不是模型质量，而是 ``copilot/catalog.py`` 根本没接入档案管理与知识库两支
模块——助手只能拿"档案/证照"当关键词跨表搜，搜不到就答"没有"。

本文件锁住三件事：
1. RBAC 里有的业务模块，catalog 必须都有；catalog 里有的模块，question_bank
   必须有问法样本（否则"接入"只是纸面上的）；
2. 样本库里的每一条问法都必须识别成它所属的模块；
3. 状态型问题（已过期/借出中/逾期）在本地说得对，不进模型。
"""

import json
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts import registry as rbac
from ai_api.models import AiProviderConfig
from archive.models import Archive, BorrowRecord, License, LicenseBorrowRecord
from copilot import catalog, registry
from copilot.engine import planner
from copilot.models import CopilotConversation, CopilotPolicy, CopilotRun
from copilot.question_bank import QUESTIONS
from knowledge_base.models import KnowledgeDocument

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
def _plain(text):
    """回答里的 markdown 记号（**加粗**）去掉，只比对人话。"""
    return (text or '').replace('**', '')

User = get_user_model()

# RBAC 里属于"业务数据"的模块（其余是系统/通知/AI 自身）
BUSINESS_MODULES = {
    'daily_record', 'decoration', 'customer_credit', 'customer_complaint',
    'customer_ledger', 'complaint', 'regulation', 'price_evaluation',
    'insurance', 'metal_price', 'knowledge', 'samples', 'archive',
}


class ModuleCatalogCoverageTests(TestCase):
    """模块目录是"AI 能看到什么"的唯一配置源，不许有漏。"""

    def test_every_rbac_business_module_is_in_catalog(self):
        covered = {catalog.perm_of(spec) for spec in catalog.MODULES}
        self.assertEqual(BUSINESS_MODULES - covered, set())

    def test_every_catalog_module_has_question_samples(self):
        missing = {m.key for m in catalog.MODULES} - set(QUESTIONS)
        self.assertEqual(missing, set())

    def test_module_keys_are_unique(self):
        keys = [m.key for m in catalog.MODULES]
        self.assertEqual(len(keys), len(set(keys)))

    def test_every_module_resolves_route_or_reports_none(self):
        """详情路由要么能反解，要么明确为空——写错路由等于引用卡片点不开。"""
        for spec in catalog.MODULES:
            if not spec.detail_route:
                continue
            with self.subTest(module=spec.key):
                path = catalog.safe_reverse(spec.detail_route, [1])
                self.assertTrue(path, spec.detail_route)


class QuestionBankTests(TestCase):
    """样本库的每条问法都要落到自己的模块上。"""

    def setUp(self):
        self.admin = User.objects.create_superuser('q_admin', 'q@x.com', 'p')
        self.policy = CopilotPolicy()
        self.modules = registry.available_modules(self.admin, True, self.policy)

    def test_all_question_samples_map_to_their_module(self):
        failures = []
        for key, questions in QUESTIONS.items():
            spec = catalog.get_module(key)
            self.assertIsNotNone(spec, key)
            for question in questions:
                got = planner.pick_module(question, self.modules, self.admin)
                if got is None or got.key != key:
                    failures.append((key, question, got.key if got else None))
        self.assertEqual(failures, [])

    def test_archive_question_recognised(self):
        spec, keyword, state = planner.module_query(
            '已借出的档案有哪些', self.modules, self.admin)
        self.assertEqual(spec.key, 'archive')
        self.assertEqual(keyword, '')
        self.assertIsNotNone(state)
        self.assertEqual(state.label, '借出中')

    def test_license_expired_question_recognised(self):
        spec, keyword, state = planner.module_query(
            '证照管理中 已过期的有哪些', self.modules, self.admin)
        self.assertEqual(spec.key, 'license')
        self.assertIsNotNone(state)
        self.assertEqual(state.label, '已过期')

    def test_borrow_module_beats_archive_when_word_is_longer(self):
        """「档案借阅记录」问的是借阅表，不是档案表。"""
        spec, _keyword, _state = planner.module_query(
            '档案借阅记录有哪些', self.modules, self.admin)
        self.assertEqual(spec.key, 'archive_borrow')

    def test_license_borrow_beats_archive_borrow(self):
        spec, _keyword, _state = planner.module_query(
            '证照借阅记录有哪些', self.modules, self.admin)
        self.assertEqual(spec.key, 'license_borrow')

    def test_generic_suffix_does_not_steal_the_module(self):
        """「样品台账中小批量的有哪些」曾答成客户台账。

        "台账"是客户台账的别名，可它在这里只是"XX台账"的通用后缀；
        打分不能只看词长与位置，泛词必须排在具体业务词之后。
        """
        spec, keyword, _state = planner.module_query(
            '样品台账中小批量的有哪些', self.modules, self.admin)
        self.assertEqual(spec.key, 'sample_batch')
        self.assertEqual(keyword, '')

    def test_generic_suffix_keeps_the_specific_module(self):
        cases = (
            ('样品台账', 'sample'),
            ('试样台账', 'sample_test'),
            ('小批量台账', 'sample_batch'),
            ('客户台账有哪些', 'customer_ledger'),
            ('台账里的客户', 'customer_ledger'),
        )
        for question, expected in cases:
            spec = planner.pick_module(question, self.modules, self.admin)
            self.assertIsNotNone(spec, question)
            self.assertEqual(spec.key, expected, question)

    def test_year_scope_is_not_turned_into_state(self):
        """「今年过期的证照」不能简化成"截至今天已过期"。"""
        _spec, _keyword, state = planner.module_query(
            '今年过期的证照有哪些', self.modules, self.admin)
        self.assertIsNone(state)


class StateFilterTests(TestCase):
    """状态不是字段：证照"已过期"要看有效期和年审日，档案"已借出"要看借阅表。"""

    def setUp(self):
        self.admin = User.objects.create_superuser('st2_admin', 'st2@x.com', 'p')
        self.policy = CopilotPolicy()
        self.conversation = CopilotConversation.objects.create(user=self.admin)
        self.run = CopilotRun.objects.create(
            conversation=self.conversation, user=self.admin, question='q')
        self.today = timezone.localdate()

    def ctx(self):
        return registry.ToolContext(
            user=self.admin, conversation=self.conversation, run=self.run,
            is_local=True, policy=self.policy)

    def test_license_expired_covers_review_date(self):
        expired_by_expire = License.objects.create(
            name='营业执照', code='LIC-T1',
            expire_date=self.today - timedelta(days=10))
        expired_by_review = License.objects.create(
            name='资质证书', code='LIC-T2',
            expire_date=self.today + timedelta(days=800),
            next_review_date=self.today - timedelta(days=10))
        License.objects.create(
            name='正常证书', code='LIC-T3',
            expire_date=self.today + timedelta(days=800))
        result = registry.run_tool(registry.get('query_records'), self.ctx(),
                                   {'module': 'license', 'state': '已过期'})
        self.assertTrue(result.ok)
        self.assertEqual(result.data['total'], 2)
        self.assertEqual(result.data['state'], '已过期')
        ids = {row['id'] for row in result.data['rows']}
        self.assertEqual(ids, {expired_by_expire.pk, expired_by_review.pk})

    def test_license_expiring_excludes_expired(self):
        License.objects.create(name='旧证', code='LIC-T4',
                               expire_date=self.today - timedelta(days=1))
        soon = License.objects.create(name='临期证', code='LIC-T5',
                                      expire_date=self.today + timedelta(days=5))
        result = registry.run_tool(registry.get('query_records'), self.ctx(),
                                   {'module': 'license', 'state': '即将到期'})
        self.assertTrue(result.ok)
        self.assertEqual([row['id'] for row in result.data['rows']], [soon.pk])

    def test_license_borrowed_uses_open_borrow_records(self):
        borrowed = License.objects.create(name='借出证', code='LIC-T6',
                                          expire_date=self.today + timedelta(days=400))
        idle = License.objects.create(name='在库证', code='LIC-T7',
                                      expire_date=self.today + timedelta(days=400))
        LicenseBorrowRecord.objects.create(license=borrowed, borrower='张三',
                                           due_time=self.today + timedelta(days=5))
        LicenseBorrowRecord.objects.create(license=idle, borrower='李四',
                                           due_time=self.today - timedelta(days=5),
                                           return_time=timezone.now())
        result = registry.run_tool(registry.get('query_records'), self.ctx(),
                                   {'module': 'license', 'state': '已借出'})
        self.assertTrue(result.ok)
        self.assertEqual([row['id'] for row in result.data['rows']], [borrowed.pk])

    def test_archive_borrowed_not_left_join_duplicates(self):
        """没有借阅记录的档案不能被当成"借出中"（LEFT JOIN 全 NULL 的坑）。"""
        borrowed = Archive.objects.create(code='DA-T1', name='借出档案')
        Archive.objects.create(code='DA-T2', name='在库档案')
        BorrowRecord.objects.create(archive=borrowed, borrower='王五',
                                    due_time=self.today + timedelta(days=3))
        result = registry.run_tool(registry.get('query_records'), self.ctx(),
                                   {'module': 'archive', 'state': '已借出'})
        self.assertTrue(result.ok)
        self.assertEqual([row['id'] for row in result.data['rows']], [borrowed.pk])

    def test_borrow_overdue_needs_unreturned(self):
        archive = Archive.objects.create(code='DA-T3', name='逾期档案')
        returned = Archive.objects.create(code='DA-T4', name='已还档案')
        BorrowRecord.objects.create(archive=archive, borrower='赵六',
                                    due_time=self.today - timedelta(days=3))
        BorrowRecord.objects.create(archive=returned, borrower='钱七',
                                    due_time=self.today - timedelta(days=3),
                                    return_time=timezone.now())
        result = registry.run_tool(registry.get('query_records'), self.ctx(),
                                   {'module': 'archive_borrow', 'state': '已逾期'})
        self.assertTrue(result.ok)
        self.assertEqual(result.data['total'], 1)
        self.assertEqual(result.data['rows'][0]['archive'][:5], 'DA-T3')

    def test_unknown_state_is_rejected_with_options(self):
        result = registry.run_tool(registry.get('query_records'), self.ctx(),
                                   {'module': 'complaint', 'state': '已过期'})
        self.assertFalse(result.ok)
        self.assertEqual(result.error, 'BAD_ARGS')
        self.assertIn('不支持', result.message)

    def test_state_count_is_local(self):
        for index in range(3):
            License.objects.create(name=f'过期{index}', code=f'LIC-C{index}',
                                   expire_date=self.today - timedelta(days=index + 1))
        License.objects.create(name='正常', code='LIC-C9',
                               expire_date=self.today + timedelta(days=500))
        stat = planner.stat_query('已过期的证照有多少份', self.modules_for(), self.admin)
        self.assertIsNotNone(stat)
        self.assertEqual(stat['state'].label, '已过期')
        answer, _refs = planner.stat_answer(stat, self.admin)
        self.assertIn('共 3 份', _plain(answer))

    def modules_for(self):
        return registry.available_modules(self.admin, True, self.policy)


class KnowledgeVisibilityTests(TestCase):
    """知识库只给"全员可见 + 本人私有"，别人的私有稿不进 AI。"""

    def setUp(self):
        self.admin = User.objects.create_superuser('kb_admin', 'kb@x.com', 'p')
        self.other = User.objects.create_user('kb_other', password='p')
        self.policy = CopilotPolicy()
        self.conversation = CopilotConversation.objects.create(user=self.admin)
        self.run = CopilotRun.objects.create(
            conversation=self.conversation, user=self.admin, question='q')
        self.pub = KnowledgeDocument.objects.create(
            title='公开流程', content='公开内容', visibility='public',
            status='published', owner=self.other)
        self.mine = KnowledgeDocument.objects.create(
            title='我的笔记', content='私有内容', visibility='private',
            status='published', owner=self.admin)
        self.theirs = KnowledgeDocument.objects.create(
            title='别人的笔记', content='不该看到', visibility='private',
            status='published', owner=self.other)

    def test_private_docs_of_others_are_hidden(self):
        ctx = registry.ToolContext(
            user=self.admin, conversation=self.conversation, run=self.run,
            is_local=True, policy=self.policy)
        result = registry.run_tool(registry.get('query_records'), ctx,
                                   {'module': 'knowledge', 'limit': 50})
        self.assertTrue(result.ok)
        ids = {row['id'] for row in result.data['rows']}
        self.assertEqual(ids, {self.pub.pk, self.mine.pk})

    def test_search_knowledge_tool_is_available_now(self):
        """search_knowledge 早就写了，但 knowledge 不在 catalog 里 → 永远调不到。"""
        specs = registry.available_specs(self.admin, True, self.policy)
        self.assertIn('search_knowledge', {s.name for s in specs})

    def test_public_draft_of_others_is_hidden(self):
        """列表页只显示已发布，AI 也不能把别人的草稿捞出来。"""
        draft = KnowledgeDocument.objects.create(
            title='别人草稿', content='未定稿', visibility='public',
            status='draft', owner=self.other)
        ctx = registry.ToolContext(
            user=self.admin, conversation=self.conversation, run=self.run,
            is_local=True, policy=self.policy)
        result = registry.run_tool(registry.get('query_records'), ctx,
                                   {'module': 'knowledge', 'limit': 50})
        ids = {row['id'] for row in result.data['rows']}
        self.assertNotIn(draft.pk, ids)
        self.assertIn(self.mine.pk, ids)


class BorrowStatusDisplayTests(TestCase):
    """借阅行的状态字段会过期：回答里不能一边说"已逾期"一边逐行写"借出中"。"""

    def test_stale_status_is_overridden_in_digest(self):
        archive = Archive.objects.create(code='DA-S1', name='逾期档案')
        record = BorrowRecord.objects.create(
            archive=archive, borrower='孙八',
            due_time=timezone.localdate() - timedelta(days=5))
        self.assertEqual(record.status, 'borrowed')     # 落库值确实是旧的
        spec = catalog.get_module('archive_borrow')
        answer = planner.module_digest(
            spec,
            [{'borrower': '孙八', 'due_time': record.due_time.isoformat(),
              'return_time': None, 'status': record.status, 'id': record.pk}],
            1, {'borrower': '借阅人', 'due_time': '应归还日期', 'status': '状态'},
            state_label='已逾期')
        self.assertIn('已逾期', answer)
        self.assertIn('状态：已逾期', answer)
        self.assertNotIn('状态：借出中', answer)


@override_settings(CACHES=LOCMEM, COPILOT_INLINE_EXECUTION=True)
class ScreenshotRegressionTests(TestCase):
    """两张截图的原问法：必须本地答对，一次模型都不调。"""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_superuser('rt_admin', 'rt@x.com', 'p')
        self.client.force_login(self.user)
        self.config = AiProviderConfig.objects.create(
            purpose=AiProviderConfig.Purpose.CHAT,
            provider=AiProviderConfig.Provider.OLLAMA,
            model='test-model', is_enabled=True, is_local=True, max_tokens=256,
        )
        CopilotPolicy.objects.all().delete()
        CopilotPolicy.objects.create(rate_limit_per_10min=100, rate_limit_per_day=1000)
        today = timezone.localdate()
        self.borrowed = Archive.objects.create(code='DA-R1', name='借出档案甲')
        Archive.objects.create(code='DA-R2', name='在库档案乙')
        BorrowRecord.objects.create(archive=self.borrowed, borrower='王五',
                                    due_time=today + timedelta(days=4))
        License.objects.create(name='营业执照（甲）', code='LIC-R1',
                               expire_date=today - timedelta(days=30))
        License.objects.create(name='资质证书（乙）', code='LIC-R2',
                               expire_date=today - timedelta(days=60))
        License.objects.create(name='正常证书（丙）', code='LIC-R3',
                               expire_date=today + timedelta(days=600))

    def _ask(self, question):
        return self.client.post(
            reverse('copilot:ask'), data=json.dumps({'question': question}),
            content_type='application/json').json()['data']

    def test_borrowed_archives_answered_locally(self):
        with patch('copilot.providers.router.select', return_value=(self.config, None)), \
             patch('copilot.providers.router.chat_with',
                   side_effect=AssertionError('状态问题不应调用模型')):
            data = self._ask('已借出的档案有哪些')
        self.assertEqual(data['status'], 'succeeded')
        self.assertIn('共 1 份', _plain(data['answer']))
        self.assertIn('借出中', data['answer'])
        self.assertEqual(data['steps'][0]['module'], 'archive')
        self.assertEqual(data['steps'][0]['state'], '借出中')
        self.assertTrue(data['refs'])

    def test_expired_licenses_answered_locally(self):
        with patch('copilot.providers.router.select', return_value=(self.config, None)), \
             patch('copilot.providers.router.chat_with',
                   side_effect=AssertionError('状态问题不应调用模型')):
            data = self._ask('证照管理中 已过期的有哪些')
        self.assertEqual(data['status'], 'succeeded')
        self.assertIn('共 2 份', _plain(data['answer']))
        self.assertIn('已过期', data['answer'])
        self.assertEqual(data['steps'][0]['module'], 'license')
        self.assertEqual(len(data['refs']), 2)

    def test_capability_reply_lists_new_modules(self):
        reply = planner.instant_reply('你能查哪些模块', list(catalog.MODULES))
        for label in ('档案', '证照', '知识库'):
            self.assertIn(label, reply)


@override_settings(CACHES=LOCMEM, COPILOT_INLINE_EXECUTION=True)
class SampleLedgerRegressionTests(TestCase):
    """「样品台账中小批量的有哪些」曾答成客户台账。

    "台账"只是"XX台账"的通用后缀，却在别名表里是客户台账的主词。名字打分
    若只看长度与位置，两个 2 字词打平，选中谁全靠运气。现在泛词降权，具体
    业务词（样品/试样/小批量）优先。
    """

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_superuser('sl_admin', 'sl@x.com', 'p')
        self.client.force_login(self.user)
        self.config = AiProviderConfig.objects.create(
            purpose=AiProviderConfig.Purpose.CHAT,
            provider=AiProviderConfig.Provider.OLLAMA,
            model='test-model', is_enabled=True, is_local=True, max_tokens=256,
        )
        CopilotPolicy.objects.all().delete()
        CopilotPolicy.objects.create(rate_limit_per_10min=100, rate_limit_per_day=1000)
        from samples.models import BatchRecord, Partner, Sample
        partner = Partner.objects.create(name='宏达五金')
        sample = Sample.objects.create(
            sample_no='S900', material_name='不锈钢板', model='S304',
            sample_date=date(2026, 9, 20), supplier=partner)
        BatchRecord.objects.create(batch_no='BR-900', sample=sample, round_no=1,
                                   batch_result='ongoing')

    def _ask(self, question):
        return self.client.post(
            reverse('copilot:ask'), data=json.dumps({'question': question}),
            content_type='application/json').json()['data']

    def test_batch_question_goes_to_sample_batch_not_customer_ledger(self):
        with patch('copilot.providers.router.select', return_value=(self.config, None)), \
             patch('copilot.providers.router.chat_with',
                   side_effect=AssertionError('模块列表问题不应调用模型')):
            data = self._ask('样品台账中小批量的有哪些')
        self.assertEqual(data['status'], 'succeeded')
        self.assertIn('小批量', data['answer'])
        self.assertNotIn('客户台账', data['answer'])
        self.assertEqual(data['steps'][0]['module'], 'sample_batch')


class InstantMathTests(TestCase):
    """纯算式本地直算：模型会为了"查点什么"跑两轮跨模块搜索（截图里的 0 条 ×2）。"""

    def test_simple_arithmetic_answered_locally(self):
        self.assertEqual(planner.instant_reply('1+1等于几', []), '1+1 = 2。')
        self.assertEqual(planner.instant_reply('(12+8)*3等于多少', []), '(12+8)*3 = 60。')

    def test_date_is_not_treated_as_arithmetic(self):
        self.assertEqual(planner.math_reply('2026-10-31'), '')

    def test_business_question_is_not_arithmetic(self):
        self.assertEqual(planner.math_reply('库存 3+5 等于多少'), '')
