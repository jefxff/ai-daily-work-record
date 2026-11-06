"""诉讼案件周报 PDF 回归测试。

覆盖：周窗口口径、剔除结案、货款/非货款分组与顺序、备注列、
本周更新业务句式、导出留痕排除、视图权限与 PDF 响应。
"""

from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Permission, Role

from . import complaint_weekly as cw
from .models import Complaint, OperationLog

User = get_user_model()

FRIDAY = timezone.make_aware(datetime(2026, 11, 6, 19, 0))
WINDOW = cw.resolve_window(start='2026-11-02', end='2026-11-08')


def make_complaint(**overrides):
    values = {
        'dispute_type': '货款纠纷', 'plaintiff': '甲公司', 'defendant': '乙公司',
        'case_number': 'A-001', 'amount': Decimal('1000.00'),
        'court': '福田区人民法院', 'lawyer': '罗律师', 'progress': '立案',
        'detailed_progress': '', 'remarks': '',
    }
    values.update(overrides)
    return Complaint.objects.create(**values)


def make_log(operation, when, *, obj=None, summary='', before=None, after=None,
             identifier=None):
    log = OperationLog.objects.create(
        module_type='诉讼', operation_type=operation,
        object_identifier=identifier or (f'[诉讼]下的记录#{obj.pk}' if obj else '[诉讼]下的操作'),
        change_summary=summary, before_json=before, after_json=after,
        operator='tester',
    )
    # operation_time 是 auto_now_add，测试里要显式回写窗口内时间
    OperationLog.objects.filter(pk=log.pk).update(operation_time=when)
    return OperationLog.objects.get(pk=log.pk)


class WeekWindowTests(TestCase):
    def test_current_window_is_this_monday_to_sunday(self):
        window = cw.resolve_window(as_of=FRIDAY)
        self.assertEqual(window.label, '2026-11-02 ~ 2026-11-08')
        self.assertEqual(window.start.date().isoformat(), '2026-11-02')
        self.assertEqual(window.end_exclusive.date().isoformat(), '2026-11-09')

    def test_last_window_is_previous_full_week(self):
        window = cw.resolve_window(week='last', as_of=FRIDAY)
        self.assertEqual(window.label, '2026-10-26 ~ 2026-11-01')
        self.assertEqual(window.end_exclusive.date().isoformat(), '2026-11-02')

    def test_custom_window_and_validation(self):
        window = cw.resolve_window(start='2026-10-01', end='2026-10-03')
        self.assertEqual(window.label, '2026-10-01 ~ 2026-10-03')
        with self.assertRaises(ValueError):
            cw.resolve_window(start='2026-10-03', end='2026-10-01')
        with self.assertRaises(ValueError):
            cw.resolve_window(week='unknown', as_of=FRIDAY)


class BuildContextTests(TestCase):
    def setUp(self):
        self.goods = make_complaint(
            case_number='A-001', amount=Decimal('1000.00'),
            remarks='已查封房产', judgment_at=datetime(2026, 10, 1).date(),
        )
        self.other = make_complaint(
            dispute_type='专利纠纷', case_number='B-001', defendant='丙公司',
            amount=Decimal('2500.50'), remarks='', progress='审理中',
        )
        self.closed = make_complaint(
            case_number='C-001', defendant='丁公司', progress='结案',
            amount=Decimal('999999.00'),
        )

    def test_closed_excluded_and_group_order(self):
        ctx = cw.build_context(WINDOW)
        self.assertEqual(ctx['open_count'], 2)
        self.assertEqual(ctx['goods_count'], 1)
        self.assertEqual(ctx['others_count'], 1)
        self.assertEqual(ctx['excluded_closed'], 1)
        goods, others = ctx['groups']
        self.assertIn('二、货款纠纷明细（1 条）', goods['heading'])
        self.assertIn('三、非货款纠纷明细（1 条）', others['heading'])
        self.assertFalse(goods['break_before'])
        self.assertTrue(others['break_before'])
        self.assertEqual(goods['rows'][0]['case_number'], 'A-001')
        self.assertEqual(others['rows'][0]['case_number'], 'B-001')

    def test_subtotal_ignores_closed_cases(self):
        ctx = cw.build_context(WINDOW)
        self.assertEqual(ctx['groups'][0]['subtotal'], '¥1,000.00')
        self.assertEqual(ctx['groups'][1]['subtotal'], '¥2,500.50')

    def test_note_column_keeps_remark_and_dates(self):
        ctx = cw.build_context(WINDOW)
        self.assertEqual(
            ctx['groups'][0]['rows'][0]['note_lines'],
            ['已查封房产', '判决 2026-10-01'],
        )
        self.assertEqual(ctx['groups'][1]['rows'][0]['note_lines'], ['—'])

    def test_placeholder_values_are_preserved(self):
        make_complaint(case_number='暂无', court='暂无', lawyer='暂无',
                       defendant='戊公司', amount=Decimal('1.00'))
        ctx = cw.build_context(WINDOW)
        row = ctx['groups'][0]['rows'][1]
        self.assertEqual(row['case_number'], '暂无')
        self.assertEqual(row['court'], '暂无')
        self.assertEqual(row['lawyer'], '暂无')

    def test_empty_group_still_renders(self):
        Complaint.objects.exclude(dispute_type='货款纠纷').delete()
        ctx = cw.build_context(WINDOW)
        self.assertEqual(ctx['groups'][1]['rows'], [])
        self.assertIn('本组暂无未结案案件', cw.render_html(ctx))


class WeeklyUpdatesTests(TestCase):
    def setUp(self):
        self.case = make_complaint(case_number='83287号', remarks='')
        when = timezone.make_aware(datetime(2026, 11, 4, 10, 0))
        make_log('编辑', when, obj=self.case, summary='编辑诉讼: 83287号',
                 before={'remarks': '', 'detailed_progress': ''},
                 after={'remarks': '已申请强制执行', 'detailed_progress': '等待法院判决结果'})
        make_log('编辑', when, obj=self.case,
                 before={'court': '暂无'}, after={'court': '福田区人民法院'})
        make_log('导出', when, summary='导出诉讼: 3 条')
        make_log('导入', when, summary='导入诉讼记录: 新增2 更新1 跳过0')

    def test_business_sentences_and_export_exclusion(self):
        updates = cw._build_updates(WINDOW)
        joined = ' '.join(updates)
        self.assertIn('83287号 案件更新进度：已申请强制执行', joined)
        self.assertIn('83287号 案件更正：受理法院由「暂无」更正为「福田区人民法院」', joined)
        self.assertIn('本周导入 1 批：新增 2 条、更新 1 条、跳过 0 条', joined)
        self.assertNotIn('导出', joined)
        # 同一条留痕只写一句，不把备注和详细进度拆成两条
        self.assertEqual(sum('案件更新进度' in item for item in updates), 1)

    def test_deleted_record_falls_back_to_snapshot(self):
        make_log('编辑', timezone.make_aware(datetime(2026, 11, 5, 9, 0)),
                 identifier='[诉讼]下的记录#9999',
                 before={'progress': '立案'}, after={'progress': '判决',
                                                     'case_number': 'FORM-001',
                                                     'defendant': '测试公司'})
        joined = ' '.join(cw._build_updates(WINDOW))
        self.assertIn('FORM-001 案件更新进度：进度由「立案」变更为「判决」', joined)
        self.assertNotIn('[诉讼]下的记录#9999', joined)

    def test_week_outside_window_is_empty(self):
        other = cw.resolve_window(start='2026-10-01', end='2026-10-07')
        self.assertEqual(cw._build_updates(other), [])


class WeeklyPdfViewTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command('sync_rbac', verbosity=0)

    def setUp(self):
        self.user = User.objects.create_user(
            username=f'weekly_{self._testMethodName}', password='pass12345'
        )
        role = Role.objects.create(code=f'weekly-{self.user.username}', name='诉讼周报测试角色')
        role.permissions.set(Permission.objects.filter(code='complaint.read'))
        role.users.add(self.user)
        cache.clear()
        self.client.force_login(self.user)
        make_complaint(case_number='A-001', amount=Decimal('1000.00'))

    def test_html_preview_contains_all_sections(self):
        response = self.client.get(reverse('complaint_weekly_pdf'), {'html': '1'})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        for text in ['诉讼案件周报（', '一、本周更新内容', '已剔除已结案',
                     '二、货款纠纷明细', '三、非货款纠纷明细', '备注', '小计']:
            self.assertIn(text, html)
        self.assertNotIn('编制说明', html)
        self.assertNotIn('列定义', html)

    def test_pdf_response_headers_and_audit_log(self):
        with patch.object(cw, 'render_pdf', return_value=b'%PDF-1.4 test') as mocked:
            response = self.client.get(reverse('complaint_weekly_pdf'))
        self.assertTrue(mocked.called)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn('complaint-weekly.pdf', response['Content-Disposition'])
        self.assertIn('.pdf', response['Content-Disposition'])
        self.assertEqual(response.content, b'%PDF-1.4 test')
        self.assertTrue(OperationLog.objects.filter(
            module_type='诉讼', operation_type='导出',
            change_summary__contains='导出诉讼案件周报PDF',
        ).exists())

    def test_week_last_changes_title(self):
        expected = cw.resolve_window(week='last').label
        response = self.client.get(reverse('complaint_weekly_pdf'),
                                   {'html': '1', 'week': 'last'})
        self.assertIn(f'诉讼案件周报（{expected}）', response.content.decode('utf-8'))

    def test_custom_range_and_bad_date(self):
        response = self.client.get(reverse('complaint_weekly_pdf'),
                                   {'html': '1', 'start': '2026-10-01', 'end': '2026-10-07'})
        self.assertIn('诉讼案件周报（2026-10-01 ~ 2026-10-07）',
                      response.content.decode('utf-8'))
        self.assertEqual(
            self.client.get(reverse('complaint_weekly_pdf'), {'start': 'bad'}).status_code, 400
        )

    def test_anonymous_redirected_to_login(self):
        self.client.logout()
        response = self.client.get(reverse('complaint_weekly_pdf'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])

    def test_real_pdf_generation(self):
        """不打桩，验证 Chromium 真能产出可打开的 PDF。"""
        response = self.client.get(reverse('complaint_weekly_pdf'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b'%PDF'))
        self.assertGreater(len(response.content), 10000)
