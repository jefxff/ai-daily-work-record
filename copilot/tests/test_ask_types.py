"""问法类型覆盖：排序/极值、求和、纯实体检索。

用户 2026-10-31 反馈："我问知识库中阅读最多的一篇文章，他就不知道了"。

根因不是模型笨，是意图识别缺了一整类：``planner`` 只会"找记录""数条数""
按字段分组"，没有"排个序取第一条"。于是"阅读最多"被当成关键词去全文检索
——它不是任何字段里存的值，必然 0 条，最后答成"没有符合条件的记录"，
把"我不会排序"说成了"没有这条数据"。

同一批补上的还有：
* 求和（"保费一共多少钱"）：以前交给模型，它会把条数当金额；
* 纯实体（"王国军""DA-0050"）：以前花两轮工具调用 + 一次模型调用换一句
  "没有找到"，现在本地一遍 SQL，连"搜过哪些模块"都说得清。
"""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from archive.models import Archive
from copilot import catalog, registry
from copilot.engine import planner
from copilot.models import CopilotPolicy
from knowledge_base.models import KnowledgeDocument
from records.models import MetalPrice

User = get_user_model()


class AskTypeBase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('ask_admin', 'ask@x.com', 'p')
        self.policy = CopilotPolicy()
        self.modules = registry.available_modules(self.admin, True, self.policy)

    def spec(self, key):
        spec = catalog.get_module(key)
        self.assertIsNotNone(spec, key)
        return spec


class RankQueryTests(AskTypeBase):
    """「阅读最多的一篇」= 排序取第一条，不是全文检索。"""

    def setUp(self):
        super().setUp()
        for title, views in (('冷门文档', 3), ('冠军文档', 288), ('中游文档', 40)):
            KnowledgeDocument.objects.create(
                title=title, content='正文', status='published', visibility='public',
                owner=self.admin, view_count=views)

    def test_most_read_article_is_answered_locally(self):
        rank = planner.rank_query('知识库中阅读最多的一篇文章是哪篇文章',
                                  self.modules, self.admin)
        self.assertIsNotNone(rank, '「阅读最多」必须识别成排序意图')
        self.assertEqual(rank['spec'].key, 'knowledge')
        self.assertEqual(rank['field'], 'view_count')
        self.assertTrue(rank['desc'])
        self.assertEqual(rank['limit'], 1)

        answer, refs = planner.rank_answer(rank, self.admin, self.policy)
        self.assertIn('冠军文档', answer)
        self.assertIn('288', answer)
        self.assertNotIn('没有', answer)
        self.assertTrue(refs)

    def test_top_n_returns_table(self):
        rank = planner.rank_query('知识库浏览量最高的前2个文档', self.modules, self.admin)
        self.assertIsNotNone(rank)
        self.assertEqual(rank['limit'], 2)
        answer, _refs = planner.rank_answer(rank, self.admin, self.policy)
        self.assertIn('| 1 |', answer)
        self.assertIn('| 2 |', answer)
        # 排行榜按浏览量从大到小，冠军在第一行
        self.assertLess(answer.index('冠军文档'), answer.index('中游文档'))

    def test_least_read_sorts_ascending(self):
        rank = planner.rank_query('知识库阅读最少的文档', self.modules, self.admin)
        self.assertIsNotNone(rank)
        self.assertFalse(rank['desc'])
        answer, _refs = planner.rank_answer(rank, self.admin, self.policy)
        self.assertIn('冷门文档', answer)

    def test_generic_noun_is_not_taken_as_filter(self):
        """「文档」「文章」是泛称，不能变成 icontains 条件把结果筛成 0 条。"""
        rank = planner.rank_query('知识库中阅读最多的一篇文章是哪篇文章',
                                  self.modules, self.admin)
        self.assertEqual(rank['keyword'], '')

    def test_generic_noun_of_module_is_not_a_filter(self):
        """「额度最高的客户」里的"客户"只是"客户台账"的片段，不是筛选条件。"""
        rank = planner.rank_query('额度最高的客户', self.modules, self.admin)
        self.assertIsNotNone(rank)
        self.assertEqual(rank['spec'].key, 'customer_ledger')
        self.assertEqual(rank['keyword'], '')

    def test_date_extreme_falls_back_to_date_field(self):
        """没点字段名时，"最早/最新"指向模块的日期字段。"""
        spec = self.spec('metal_price')
        self.assertTrue(spec.date_field)
        rank = planner.rank_query('最早的金属价格是哪条', self.modules, self.admin)
        self.assertIsNotNone(rank)
        self.assertEqual(rank['field'], spec.date_field)
        self.assertFalse(rank['desc'])

    def test_rank_fields_all_exist_on_models(self):
        """别名写错字段名不会报错，只会静默失效——用反射兜住。"""
        for spec in catalog.MODULES:
            for entry in spec.rank_fields:
                name = entry[0]
                with self.subTest(module=spec.key, field=name):
                    model = catalog.model_of(spec)
                    self.assertTrue(
                        any(f.name == name for f in model._meta.concrete_fields),
                        f'{spec.key}.{name} 不存在')


class SumQueryTests(AskTypeBase):
    """「一共多少钱」要的是加起来的结果，不是条数。"""

    def setUp(self):
        super().setUp()
        for index, price in enumerate(('100', '200', '301.5'), start=1):
            MetalPrice.objects.create(
                metal_name=f'牌号{index}', price=Decimal(price), unit='元/吨',
                crawl_date=date(2026, 10, index))

    def test_sum_is_computed_locally(self):
        intent = planner.sum_query('金属价格合计多少钱', self.modules, self.admin)
        self.assertIsNotNone(intent)
        self.assertEqual(intent['field'], 'price')
        answer, _refs = planner.sum_answer(intent, self.admin, self.policy)
        self.assertIn('601.5', answer)
        self.assertIn('合计', answer)

    def test_sum_ignores_time_scope(self):
        """带"本月/今年"的求和口径各模块不同，仍交给模型按日期区间算。"""
        self.assertIsNone(
            planner.sum_query('本月金属价格合计多少钱', self.modules, self.admin))

    def test_ambiguous_amount_field_is_not_guessed(self):
        """模块有多个金额字段而问题没指明时，不乱挑一个求和。"""
        self.assertIsNone(planner.sum_query('客诉一共多少钱', self.modules, self.admin))


class EntityQueryTests(AskTypeBase):
    """「王国军」这类查询：本地搜，别让模型猜，也别把"没这条数据"说成"不会查"。"""

    def setUp(self):
        super().setUp()
        Archive.objects.create(code='DA-0001', name='工程图纸')

    def test_unknown_person_explains_search_scope(self):
        got = planner.entity_query('王国军', self.modules, self.admin, self.policy)
        self.assertIsNotNone(got)
        answer, refs = got
        self.assertEqual(refs, [])
        self.assertIn('王国军', answer)
        # 要说出"搜过多少个模块"，用户才知道这不是敷衍
        self.assertIn(str(len(self.modules)), answer)

    def test_code_hits_the_record(self):
        got = planner.entity_query('DA-0001', self.modules, self.admin, self.policy)
        self.assertIsNotNone(got)
        _answer, refs = got
        self.assertTrue(refs)
        self.assertEqual(refs[0]['module'], 'archive')

    def test_concept_question_still_goes_to_model(self):
        """「报销流程是什么」不是"找个东西"，本地检索说不清，交回模型。"""
        self.assertIsNone(
            planner.entity_query('报销流程是什么', self.modules, self.admin, self.policy))

    def test_question_with_module_name_skips_entity_path(self):
        """点明了模块就按模块查（"客户台账里的王国军"不该走全模块乱搜）。"""
        self.assertIsNone(
            planner.entity_query('客户台账里的王国军', self.modules, self.admin, self.policy))
