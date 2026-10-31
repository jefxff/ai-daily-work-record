"""回答排版与识别精度的回归测试。

两块内容：

1. **Markdown 渲染**：回答正文现在会按 Markdown 渲染成 HTML 塞进面板，
   模型输出是不可信文本——``<script>``、``javascript:`` 链接必须被剥掉，
   否则等于把 XSS 口子开在聊天框里。
2. **识别精度**：评估矩阵（``manage.py copilot_eval``）跑出来的那几类问法，
   每条都在这里落一个断言，防止以后改别名表时又退化：
   - "…按X分别有多少" 要识别成分组；
   - "…有多少"（不带量词）要识别成计数；
   - 选项标签互为子串（通过 / 有条件通过）不能算成"两个标签"；
   - 字段值精确匹配："元/吨" 不能把 "美元/吨" 算进来。
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from copilot import markdown_render, registry, security
from copilot.engine import planner
from copilot.models import CopilotConversation, CopilotPolicy, CopilotRun
from copilot.tests.test_stats import _plain

User = get_user_model()


class MarkdownRenderTests(TestCase):
    def test_table_and_bold_render_to_html(self):
        html = markdown_render.render('共 **12 家**\n\n| 分类 | 数量 |\n| --- | --- |\n| 普通 | 7 家 |')
        self.assertIn('<strong>12 家</strong>', html)
        self.assertIn('<table>', html)
        self.assertIn('<td>普通</td>', html)

    def test_script_and_event_handlers_are_stripped(self):
        html = markdown_render.render('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>')
        self.assertNotIn('<script', html)
        self.assertNotIn('onerror', html)
        self.assertNotIn('<img', html)

    def test_javascript_link_is_removed(self):
        html = markdown_render.render('[点我](javascript:alert(1))')
        self.assertNotIn('javascript:', html)
        # 站内链接要保留（引用卡片之外的正文链接也是站内的）
        self.assertIn('href="/samples/1"',
                      markdown_render.render('[样品](/samples/1)'))

    def test_plain_text_version_keeps_numbers(self):
        plain = markdown_render.to_plain('共 **12 家**')
        self.assertEqual(plain, '共 12 家')


class AnswerHtmlContractTests(TestCase):
    """接口必须下发渲染好的 HTML，前端不允许自己解析模型原文。"""

    def setUp(self):
        self.user = User.objects.create_superuser('md_admin', 'md@x.com', 'p')
        self.conversation = CopilotConversation.objects.create(user=self.user)
        self.run = CopilotRun.objects.create(
            conversation=self.conversation, user=self.user, question='q',
            answer='「小批量」共 **50 条**\n\n- **BR-0039** · 批次 4',
            status=CopilotRun.Status.SUCCEEDED)

    def test_run_data_carries_answer_html(self):
        from copilot.views import _run_data
        data = _run_data(self.run)
        self.assertEqual(data['answer'], self.run.answer)          # 原文保留
        self.assertIn('<strong>50 条</strong>', data['answer_html'])
        self.assertIn('<li>', data['answer_html'])

    def test_history_carries_content_html_for_assistant_only(self):
        from copilot.views import _conversation_data
        self.conversation.messages.create(role='assistant', content='共 **3 条**')
        self.conversation.messages.create(role='user', content='**原始输入**')
        data = _conversation_data(self.conversation)
        assistant = [m for m in data['messages'] if m['role'] == 'assistant'][0]
        user = [m for m in data['messages'] if m['role'] == 'user'][0]
        self.assertIn('<strong>3 条</strong>', assistant['content_html'])
        self.assertEqual(user['content_html'], '')


class RecognitionPrecisionTests(TestCase):
    """评估矩阵里暴露过的坑，逐条锁死。"""

    def setUp(self):
        self.admin = User.objects.create_superuser('rp_admin', 'rp@x.com', 'p')
        self.policy = security.policy()
        self.modules = registry.available_modules(self.admin, True, self.policy)

    def _spec(self, key):
        return next(m for m in self.modules if m.key == key)

    def test_group_question_with_single_label(self):
        """「按客户分类分别有多少」：只提到一个分类词，也要能识别成分组。"""
        stat = planner.stat_query('客户台账按客户分类分别有多少？',
                                  self.modules, self.admin)
        self.assertIsNotNone(stat)
        self.assertEqual(stat['spec'].key, 'customer_ledger')
        self.assertEqual(stat['group_field'], 'customer_category')

    def test_count_without_unit(self):
        """「…有多少？」不带量词，也得算计数。"""
        stat = planner.stat_query('客户台账中客户分类为国企的有多少？',
                                  self.modules, self.admin)
        self.assertIsNotNone(stat)
        self.assertEqual(stat['spec'].key, 'customer_ledger')

    def test_option_label_substring_is_not_two_labels(self):
        """「有条件通过」里含"通过"，不能被当成"同字段两个标签"→分组。"""
        labels = planner.matched_labels(self._spec('sample_audit'),
                                        '审厂结果为有条件通过的有多少')
        self.assertEqual(labels.get('audit_result'), ['有条件通过'])

    def test_value_exact_match_does_not_hit_longer_value(self):
        """精确匹配：筛选"元/吨"时，"美元/吨"的记录不能算进来。"""
        from records.models import MetalPrice
        from copilot.tools.common import apply_filters
        for unit in ('元/吨', '元/吨', '美元/吨'):
            MetalPrice.objects.create(metal_name='铜', price=Decimal('70000'), unit=unit)
        spec = self._spec('metal_price')
        qs = apply_filters(MetalPrice.objects.all(), spec, keyword='元/吨',
                           keyword_field='unit')
        self.assertEqual(qs.count(), 2)
        # 不给字段时退回模糊匹配（模型可能只给了半个词）
        fuzzy = apply_filters(MetalPrice.objects.all(), spec, keyword='元/吨')
        self.assertEqual(fuzzy.count(), 3)

    def test_title_text_does_not_steal_named_module(self):
        """别的模块一条记录标题恰好含这些字，不能把点名了模块的问句抢走。"""
        found = planner.pick_module('装修工程记录', self.modules, self.admin)
        self.assertEqual(found.key, 'decoration')
        found = planner.pick_module('试样测试结果', self.modules, self.admin)
        self.assertEqual(found.key, 'sample_test')


class EvalMatrixSmokeTests(TestCase):
    def test_report_renders_markdown(self):
        from copilot import eval_matrix
        user = User.objects.create_superuser('ev_admin', 'ev@x.com', 'p')
        CopilotPolicy.objects.all().delete()
        CopilotPolicy.objects.create()
        report = eval_matrix.run_all(user)
        text = eval_matrix.render_markdown(report, user=user)
        self.assertIn('# AI Copilot 全模块问句评估报告', text)
        self.assertIn('通过率', text)
        # 演示库为空：列表/统计类用例应被标记跳过（不是判成失败），
        # 只有问句库那批"只检查识别"的用例照跑
        self.assertEqual(report.failed, 0)
        self.assertGreater(report.skipped, 0)
