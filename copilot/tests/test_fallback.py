"""模型不可用时的关键词降级检索。

用户反馈的两个问题在这里锁定：
1. 模型挂了之后「关键词兜底检索」显示未执行、什么也没返回——降级必须真给出结果；
2. 问句里点名的模块（"最近发布了哪些制度"）没有可匹配的字段值，也要列出该模块
   最新的几条，而不是一句「AI 不可用」了事。
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from copilot.engine import planner
from copilot.engine.executor import _friendly
from copilot.models import CopilotPolicy
from records.models import CustomerComplaint, Regulation

LOCMEM = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
User = get_user_model()


@override_settings(CACHES=LOCMEM)
class KeywordFallbackTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser('fb_admin', 'fb@x.com', 'p')
        self.policy = CopilotPolicy.current()
        Regulation.objects.create(title='考勤管理制度', doc_no='ZD-001',
                                  issue_date=date(2026, 9, 1), content='考勤相关')
        Regulation.objects.create(title='差旅报销管理办法', doc_no='ZD-002',
                                  issue_date=date(2026, 10, 1), content='差旅相关')
        CustomerComplaint.objects.create(
            customer_name='华建建材', complaint_date=date(2026, 9, 10),
            complaint_status='处理中')

    def fallback(self, question):
        return planner.keyword_fallback(self.admin, question, self.policy)

    def test_named_module_without_keyword_hit_lists_recent(self):
        """「最近发布了哪些规定」没有任何字段值可匹配，仍要按最新顺序给出条目。"""
        answer, refs = self.fallback('最近发布了哪些规定')
        self.assertTrue(answer)
        self.assertTrue(refs)
        self.assertEqual(refs[0]['module'], 'regulation')
        self.assertEqual(refs[0]['label'], '差旅报销管理办法')   # 最新在前
        self.assertIn('最新几条', answer)

    def test_keyword_hit_orders_by_date(self):
        """关键词命中多条时，新的排前面（降级结果也应是"最近优先"）。"""
        Regulation.objects.create(title='设备管理制度', doc_no='ZD-003',
                                  issue_date=date(2026, 10, 15), content='设备')
        answer, refs = self.fallback('有哪些制度')
        self.assertEqual(refs[0]['label'], '设备管理制度')

    def test_keyword_hit_still_wins(self):
        answer, refs = self.fallback('华建建材有哪些客户客诉')
        self.assertTrue(refs)
        self.assertEqual(refs[0]['module'], 'customer_complaint')
        self.assertIn('华建建材', answer)

    def test_no_hit_gives_actionable_message(self):
        answer, refs = self.fallback('zzz 不存在的关键词 qqq')
        self.assertEqual(refs, [])
        self.assertIn('可以查询的模块有', answer)
        self.assertIn('全站搜索', answer)

    def test_time_words_are_not_used_as_keywords(self):
        """「最近/本周」这类词不应当作检索关键词（否则会误命中任意文本）。"""
        self.assertNotIn('最近', planner.keyword_candidates('最近发布了哪些制度'))

    def test_hinted_modules_detected_by_alias(self):
        modules = planner.registry.available_modules(self.admin, True, self.policy)
        keys = [m.key for m in planner.hinted_modules('最近的诉讼进展', modules)]
        self.assertIn('complaint', keys)
        keys = [m.key for m in planner.hinted_modules('本月的工作记录', modules)]
        self.assertIn('daily_record', keys)

    def test_friendly_maps_http_status(self):
        self.assertIn('401', _friendly('HTTP_ERROR', 'HTTP 401：{"error":"Unauthorized"}'))
        self.assertIn('模型名称', _friendly('HTTP_ERROR', 'HTTP 404：model not found'))
        self.assertIn('密钥', _friendly('HTTP_ERROR', 'HTTP 401：x'))
        self.assertEqual(_friendly('NO_PROVIDER'), '未配置模型')
