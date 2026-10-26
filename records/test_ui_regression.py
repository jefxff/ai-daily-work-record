"""Phase 4 · 专项回归测试用例

验证本次前端修复（P0/P1/P2）未破坏数据相关功能与关键交互契约。

设计原则：
- 全部运行在 Django 独立测试库中，绝不触碰生产库；
- 覆盖：分页纯逻辑、各业务列表页渲染、列表读取不丢行、
  删除接口的「GET 不得删除」约束、关键无障碍标注与外观切换入口。
"""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from records.models import DailyRecord
from records.templatetags.pager_tags import page_window


class _FakePaginator:
    def __init__(self, num_pages):
        self.num_pages = num_pages


class _FakePage:
    def __init__(self, number, num_pages):
        self.number = number
        self.paginator = _FakePaginator(num_pages)


class PageWindowTests(TestCase):
    """共享分页组件依赖的页码窗口化纯逻辑。"""

    def test_first_page_window(self):
        self.assertEqual(page_window(_FakePage(1, 20)), [1, 2, 3, None, 20])

    def test_middle_page_has_ellipsis_on_both_sides(self):
        self.assertEqual(page_window(_FakePage(10, 20)), [1, None, 8, 9, 10, 11, 12, None, 20])

    def test_last_page_window(self):
        self.assertEqual(page_window(_FakePage(20, 20)), [1, None, 18, 19, 20])

    def test_single_page_has_no_ellipsis(self):
        self.assertEqual(page_window(_FakePage(1, 1)), [1])

    def test_no_ellipsis_when_all_pages_adjacent(self):
        self.assertEqual(page_window(_FakePage(2, 4)), [1, 2, 3, 4])


class UiRegressionTests(TestCase):
    """外壳与列表页回归：渲染可用、数据可读、删除受 POST 约束。"""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_superuser(
            username='regr_super', email='regr@example.com', password='pass-12345'
        )
        cls.rec = DailyRecord.objects.create(
            user=cls.user, department='测试部', work_content='回归用例内容', status='未完成', progress=0
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)

    def test_key_list_pages_render_ok(self):
        names = [
            'daily_record_list', 'decoration_list', 'customer_credit_list',
            'customer_complaint_list', 'customer_ledger_list', 'complaint_list', 'regulation_list',
            'price_evaluation_list', 'insurance_list', 'operation_log_list',
            'archive:overview', 'archive:cabinet_list', 'archive:category_list',
            'archive:borrow_list', 'archive:license_list', 'archive:license_borrow_list',
            'accounts:user_list', 'accounts:login_logs',
            'samples:dashboard', 'samples:list', 'samples:partner_list',
            'notifications:list', 'sysbackup:center',
        ]
        for name in names:
            with self.subTest(url_name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_metal_price_detail_renders(self):
        self.assertEqual(
            self.client.get(reverse('metal_price_detail', args=['黄金'])).status_code, 200
        )

    def test_list_reads_existing_row(self):
        """列表读取链路不丢数据：已存在的记录必须能在列表中呈现。"""
        resp = self.client.get(reverse('daily_record_list'))
        self.assertContains(resp, '回归用例内容')

    def test_get_must_not_delete(self):
        """删除接口只允许 POST：GET 请求不得删除任何数据。"""
        url = reverse('daily_record_delete', args=[self.rec.pk])
        self.client.get(url)
        self.assertTrue(DailyRecord.objects.filter(pk=self.rec.pk).exists())

    def test_post_delete_removes_row(self):
        rec = DailyRecord.objects.create(
            user=self.user, department='测试部', work_content='待删除用例', status='未完成', progress=0
        )
        resp = self.client.post(reverse('daily_record_delete', args=[rec.pk]))
        self.assertIn(resp.status_code, (200, 302))
        self.assertFalse(DailyRecord.objects.filter(pk=rec.pk).exists())

    def test_shell_markup_contract(self):
        """外壳关键无障碍标注与外观切换入口存在。"""
        html = self.client.get(reverse('home')).content.decode('utf-8')
        self.assertIn('id="themeToggleBtn"', html)
        self.assertIn('aria-label="全站搜索"', html)
        self.assertIn('id="samplesDropdown"', html)
        self.assertIn('aria-controls="samplesDropdownMenu"', html)
        self.assertIn('aria-controls="archiveDropdownMenu"', html)
