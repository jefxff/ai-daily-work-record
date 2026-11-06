"""诉讼导入预览「列检查」回归测试。

覆盖：表头列差异、按列汇总异常、逐条明细、阻断口径不变、错误文案不变。
"""

import io as pyio
import json
import re

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from accounts.models import Permission, Role

from . import complaint_io as cio
from .models import Complaint

User = get_user_model()

XLSX_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def make_row(**overrides):
    row = {header: '' for header in cio.COMPLAINT_HEADERS}
    row.update({
        '序号': '1', '纠纷类型': '货款纠纷', '原告': '甲公司', '被告': '乙公司',
        '案号': 'A-001', '涉案金额（元）': '1000', '受理法院': '福田区人民法院',
        '代理律师': '罗律师', '进度': '立案',
    })
    row.update(overrides)
    return row


def make_xlsx(rows, headers=None, blank_leading_col=False):
    from openpyxl import Workbook

    hdrs = list(headers or cio.COMPLAINT_HEADERS)
    wb = Workbook()
    ws = wb.active
    if blank_leading_col:
        ws.append([''] + hdrs)
        for row in rows:
            ws.append([''] + [row.get(h, '') for h in hdrs])
    else:
        ws.append(hdrs)
        for row in rows:
            ws.append([row.get(h, '') for h in hdrs])
    buf = pyio.BytesIO()
    wb.save(buf)
    return SimpleUploadedFile('complaint.xlsx', buf.getvalue(), content_type=XLSX_TYPE)


class ComplaintColumnCheckTestBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command('sync_rbac', verbosity=0)

    def setUp(self):
        self.user = User.objects.create_user(
            username=f'complaint_col_{self._testMethodName}', password='pass12345'
        )
        role = Role.objects.create(
            code=f'complaint-{self.user.username}', name='诉讼列检查测试角色'
        )
        role.permissions.set(Permission.objects.filter(code__in=[
            'complaint.read', 'complaint.create', 'complaint.update', 'complaint.delete',
        ]))
        role.users.add(self.user)
        cache.clear()
        self.client.force_login(self.user)

    def preview(self, rows, headers=None, blank_leading_col=False):
        upload = make_xlsx(rows, headers=headers, blank_leading_col=blank_leading_col)
        return self.client.post(reverse('complaint_import'), {'file': upload})

    @staticmethod
    def payload_from(html):
        import html as html_mod

        match = re.search(r'name="payload" value="(.*?)"', html, re.S)
        return html_mod.unescape(match.group(1)) if match else ''


class RowErrorTextTests(ComplaintColumnCheckTestBase):
    def test_error_texts_unchanged(self):
        """结构化改造后逐行错误文案逐字不变。"""
        row = make_row(代理律师='', 受理法院='', 案号='', **{'涉案金额（元）': '/', '进度': '未知状态'})
        values = cio.row_to_values(row)
        texts = cio._row_errors(2, values, row)
        self.assertIn('第 2 行：案号为空', texts)
        self.assertIn('第 2 行：受理法院为空', texts)
        self.assertIn('第 2 行：代理律师为空', texts)
        self.assertIn('第 2 行：涉案金额无法识别为数值', texts)
        self.assertIn(
            '第 2 行：进度「未知状态」不是有效状态（可选：立案 / 审理中 / 判决 / 执行 / 结案）',
            texts,
        )
        self.assertEqual(len(texts), 5)

    def test_missing_amount_text_unchanged(self):
        row = make_row(**{'涉案金额（元）': ''})
        values = cio.row_to_values(row)
        self.assertIn(
            '第 2 行：涉案金额（元）为空', cio._row_errors(2, values, row)
        )


class HeaderReportTests(ComplaintColumnCheckTestBase):
    def test_standard_headers_pass(self):
        report = cio.build_header_report(list(cio.COMPLAINT_HEADERS))
        self.assertTrue(report['ok'])
        self.assertEqual(report['recognized_count'], 16)
        self.assertEqual(report['unknown'], [])
        self.assertEqual(report['missing_required'], [])

    def test_blank_leading_column(self):
        report = cio.build_header_report([''] + list(cio.COMPLAINT_HEADERS))
        self.assertEqual(report['total_columns'], 17)
        self.assertEqual(report['recognized_count'], 16)
        self.assertEqual(report['blank_columns'], ['A'])
        self.assertTrue(any('空表头列' in text for _level, text in report['issues']))
        self.assertFalse(report['order_mismatch'])

    def test_missing_required_and_unknown_columns(self):
        headers = [h for h in cio.COMPLAINT_HEADERS if h != '代理律师'] + ['自定义列']
        report = cio.build_header_report(headers)
        self.assertEqual(report['missing_required'], ['代理律师'])
        self.assertEqual(report['unknown'], ['自定义列'])
        self.assertTrue(any('缺失必填列' in text for _level, text in report['issues']))
        self.assertTrue(any('未识别列' in text for _level, text in report['issues']))

    def test_column_order_mismatch_is_reported(self):
        headers = list(cio.COMPLAINT_HEADERS)
        headers[1], headers[2] = headers[2], headers[1]
        self.assertTrue(cio.build_header_report(headers)['order_mismatch'])


class ColumnIssueTests(ComplaintColumnCheckTestBase):
    def test_issues_grouped_by_column(self):
        rows = [
            make_row(序号='1', 案号='A-001', 代理律师=''),
            make_row(序号='2', 案号='A-002', **{'涉案金额（元）': '/'}),
            make_row(序号='3', 案号='A-003'),
        ]
        preview = cio.build_preview(rows)
        groups = {(g['column'], g['kind']): g for g in preview['column_issues']}
        self.assertEqual(groups[('代理律师', '为空')]['count'], 1)
        self.assertEqual(groups[('代理律师', '为空')]['row_label'], '2')
        self.assertEqual(groups[('涉案金额（元）', '无法识别为数值')]['count'], 1)
        self.assertEqual(len(preview['issue_rows']), 2)
        self.assertEqual(preview['issue_rows'][0]['reason'], '代理律师为空')
        self.assertEqual(preview['errors'], [item['text'] for item in preview['issue_rows']])

    def test_duplicate_case_number_is_attributed_to_column(self):
        rows = [
            make_row(序号='1', 案号='A-001'),
            make_row(序号='2', 案号='A-001'),
        ]
        preview = cio.build_preview(rows)
        self.assertEqual(len(preview['errors']), 1)
        self.assertIn('重复', preview['errors'][0])
        self.assertEqual(preview['issue_rows'][0]['column'], '案号')
        self.assertEqual(preview['issue_rows'][0]['kind'], '文件内重复')
        self.assertEqual(preview['column_issues'][0]['row_label'], '3')

    def test_clean_rows_have_no_column_issues(self):
        preview = cio.build_preview([make_row()])
        self.assertEqual(preview['errors'], [])
        self.assertEqual(preview['column_issues'], [])
        self.assertEqual(preview['issue_rows'], [])


class PreviewPageTests(ComplaintColumnCheckTestBase):
    def test_column_check_block_is_rendered_above_errors(self):
        rows = [
            make_row(序号='1', 案号='A-001'),
            make_row(序号='2', 案号='A-002', 代理律师=''),
            make_row(序号='3', 案号='A-003', **{'涉案金额（元）': '/'}),
        ]
        resp = self.preview(rows, blank_leading_col=True)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode('utf-8')
        self.assertIn('列检查', html)
        self.assertIn('识别到 16 / 17 列', html)
        self.assertIn('空表头列 1 个（A 列）', html)
        self.assertIn('异常列', html)
        self.assertIn('代理律师', html)
        self.assertIn('涉案金额（元）', html)
        self.assertIn('逐条明细', html)
        self.assertIn('查看上方列检查', html)
        self.assertLess(html.find('id="column-check"'), html.find('id="import-errors"'))
        self.assertIn('disabled title="请先修正阻断错误"', html)

    def test_confirm_still_blocked_by_errors(self):
        rows = [make_row(代理律师='')]
        html = self.preview(rows).content.decode('utf-8')
        payload = self.payload_from(html)
        self.assertTrue(payload)
        resp = self.client.post(reverse('complaint_import_confirm'), {
            'payload': payload, 'overwrite': '0',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn('导入数据校验未通过', resp.content.decode('utf-8'))
        self.assertEqual(Complaint.objects.count(), 0)

    def test_clean_file_passes_check_and_imports(self):
        html = self.preview([make_row()], blank_leading_col=True).content.decode('utf-8')
        self.assertIn('数据列检查通过', html)
        self.assertNotIn('disabled title="请先修正阻断错误"', html)
        payload = self.payload_from(html)
        resp = self.client.post(reverse('complaint_import_confirm'), {
            'payload': payload, 'overwrite': '0',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn('导入结果', resp.content.decode('utf-8'))
        self.assertEqual(Complaint.objects.count(), 1)

    def test_payload_round_trip_keeps_rows(self):
        rows = [make_row(案号='A-001'), make_row(序号='2', 案号='A-002')]
        html = self.preview(rows).content.decode('utf-8')
        self.assertEqual(len(json.loads(self.payload_from(html))), 2)
