"""客户评审 / 付款穿行测试：Excel 导入导出与通用附件接入测试。

覆盖三条容易回归的边界：
1. 结果列（合计得分 / 等级 / 审核结论 / 测试结论）必须服务端重算，Excel 值不生效；
2. 导入是「先预览、再确认」的两段式，阻断错误不得写库；
3. 附件按业务模块鉴权，且随记录删除一并清理。
"""

import io
import re

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from openpyxl import Workbook, load_workbook

from accounts.models import Permission, Role
from attachments.models import Attachment

from . import credit_spec as spec
from . import customer_review_io as rio
from .models import CustomerReview, PaymentWalkthrough

User = get_user_model()


def grant(user, *actions):
    role = Role.objects.create(code=f'review-{user.username}', name='客户评审测试角色')
    role.permissions.set(
        Permission.objects.filter(code__in=[f'customer_review.{a}' for a in actions])
    )
    role.users.add(user)
    cache.clear()
    return role


def as_xlsx(wb):
    buf = io.BytesIO()
    wb.save(buf)
    return SimpleUploadedFile(
        'review.xlsx', buf.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )


def credit_workbook(**overrides):
    """按导入模板列序生成一份信用评价表。"""
    columns = [c[0] for c in rio.review_columns(CustomerReview.TYPE_CREDIT)]
    data = {
        '表单编号': 'T-CR-001',
        '客户全称': '测试导入公司',
        '客户类型': '经销商',
        '表单日期': '2026-09-30',
        '资产总额（万元）': '10000',
        '负债总额（万元）': '4000',
        '流动资产（万元）': '6000',
        '流动负债（万元）': '3000',
        '营业收入（本期）（万元）': '12000',
        '净利润（万元）': '600',
        '基本情况·注册资本规模': '5',
        '基本情况·企业成立年限': '4',
        '合计得分': '100',      # 应被忽略
        '评定等级': 'A 级',     # 应被忽略
    }
    data.update(overrides)
    wb = Workbook()
    ws = wb.active
    ws.title = '信用评价'
    ws.append(columns)
    ws.append([data.get(c, '') for c in columns])
    return wb


class ReviewIoTestBase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command('sync_rbac', verbosity=0)

    def setUp(self):
        self.user = User.objects.create_user(username='review_user', password='pass12345')
        grant(self.user, 'read', 'create', 'update', 'delete')
        self.client.force_login(self.user)

    def make_credit(self, **overrides):
        data = {
            'review_type': CustomerReview.TYPE_CREDIT,
            'review_no': 'T-CR-BASE',
            'customer_name': '基数客户',
            'review_date': '2026-10-01',
            'total_assets': 8000, 'total_liabilities': 4000,
            'current_assets': 5000, 'current_liabilities': 2500,
            'item_scores': {'g0i0': 5}, 'total_score': 5, 'grade': 'D 级',
        }
        data.update(overrides)
        return CustomerReview.objects.create(**data)


class ReviewExportTests(ReviewIoTestBase):
    def test_list_export_splits_sheets_by_type(self):
        self.make_credit()
        CustomerReview.objects.create(
            review_type=CustomerReview.TYPE_AUDIT, customer_name='审核客户',
            review_date='2026-10-02', audit_verdicts={'r0': '符合'},
        )
        resp = self.client.get(reverse('customer_review_export'))
        self.assertEqual(resp.status_code, 200)
        wb = load_workbook(io.BytesIO(resp.content))
        self.assertEqual(wb.sheetnames[:3], ['信用评价', '定期考评', '现场审核'])

        headers = [c.value for c in wb['信用评价'][1]]
        self.assertIn('客户全称', headers)
        self.assertIn('资产负债率（%）', headers)
        self.assertEqual(
            sum(1 for c in rio.review_columns(CustomerReview.TYPE_CREDIT) if c[2] == 'score'),
            sum(len(g['items']) for g in spec.CREDIT_GROUPS),
        )

    def test_export_respects_type_filter(self):
        self.make_credit()
        resp = self.client.get(reverse('customer_review_export'),
                               {'review_type': CustomerReview.TYPE_AUDIT})
        # 只剩现场审核有数据也应正常出包（空表保留表头，便于当模板用）
        self.assertEqual(resp.status_code, 200)

    def test_single_export_returns_form_layout(self):
        obj = self.make_credit()
        resp = self.client.get(reverse('customer_review_export_one', args=[obj.pk]))
        self.assertEqual(resp.status_code, 200)
        ws = load_workbook(io.BytesIO(resp.content)).active
        flat = [str(c.value) for row in ws.iter_rows() for c in row if c.value is not None]
        self.assertTrue(any('客户信用评价' in v for v in flat))
        self.assertIn('评分项', flat)

    def test_template_download(self):
        resp = self.client.get(reverse('customer_review_template'))
        self.assertEqual(resp.status_code, 200)
        wb = load_workbook(io.BytesIO(resp.content))
        self.assertEqual(wb.sheetnames[:3], ['信用评价', '定期考评', '现场审核'])


class ReviewImportTests(ReviewIoTestBase):
    def _preview(self, upload):
        resp = self.client.post(reverse('customer_review_import'), {'file': upload})
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        match = re.search(r'name="payload" value="([^"]*)"', body)
        return body, (match.group(1) if match else '')

    def test_import_recomputes_score_and_grade(self):
        body, payload = self._preview(as_xlsx(credit_workbook()))
        self.assertIn('新增 1', body)
        import html as html_mod

        resp = self.client.post(reverse('customer_review_import_confirm'),
                                {'payload': html_mod.unescape(payload), 'overwrite': '0'})
        self.assertEqual(resp.status_code, 200)

        obj = CustomerReview.objects.get(review_no='T-CR-001')
        # Excel 里写的是 100 分 A 级，实际只填了 2 个评分项 → 必须重算
        self.assertEqual(float(obj.total_score), 9.0)
        self.assertTrue(obj.grade.startswith('D'))
        # 比率按基础数据重算落库
        self.assertEqual(str(obj.debt_ratio), '40.00')
        self.assertEqual(str(obj.current_ratio), '2.00')

    def test_import_blocks_on_missing_customer(self):
        wb = credit_workbook(**{'客户全称': ''})
        body, _payload = self._preview(as_xlsx(wb))
        self.assertIn('客户全称为空', body)
        self.assertEqual(CustomerReview.objects.count(), 0)

    def test_import_updates_existing_record(self):
        self.make_credit(review_no='T-CR-001', customer_name='测试导入公司')
        _body, payload = self._preview(as_xlsx(credit_workbook(客户编码='C-9')))
        import html as html_mod

        self.client.post(reverse('customer_review_import_confirm'),
                         {'payload': html_mod.unescape(payload), 'overwrite': '0'})
        obj = CustomerReview.objects.get(review_no='T-CR-001')
        self.assertEqual(obj.customer_code, 'C-9')
        self.assertEqual(float(obj.total_score), 9.0)

    def test_import_requires_permission(self):
        other = User.objects.create_user(username='reader_only', password='pass12345')
        grant(other, 'read')
        self.client.force_login(other)
        resp = self.client.post(reverse('customer_review_import'),
                                {'file': as_xlsx(credit_workbook())})
        self.assertNotEqual(resp.status_code, 200)


class WalkthroughImportTests(ReviewIoTestBase):
    def _build(self):
        main_cols = [c[0] for c in rio.WT_MAIN_COLUMNS]
        sample_cols = [c[0] for c in rio.WT_SAMPLE_COLUMNS]
        wb = Workbook()
        ws = wb.active
        ws.title = '测试清单'
        ws.append(main_cols)
        main = {
            '测试编号': 'T-WT-001', '被测试对象': '测试供应商', '对象类型': '供应商',
            '测试期间起': '2026-01-01', '测试期间止': '2026-06-30',
            '测试执行日期': '2026-11-01', '总体笔数': '80', '抽样笔数': '2',
            '测试结论': '有效',   # 应被重算覆盖
        }
        ws.append([main.get(c, '') for c in main_cols])

        ws2 = wb.create_sheet('样本明细')
        ws2.append(sample_cols)
        for seq, step, result in ((1, 's1', '执行'), (1, 's2', '未执行'), (2, 's1', '执行')):
            row = {'测试编号': 'T-WT-001', '样本序号': seq,
                   '控制点编号': step, '执行结果': result}
            ws2.append([row.get(c, '') for c in sample_cols])
        return as_xlsx(wb)

    def test_import_groups_samples_and_recomputes_conclusion(self):
        resp = self.client.post(reverse('walkthrough_import'), {'file': self._build()})
        body = resp.content.decode()
        match = re.search(r'name="payload" value="([^"]*)"', body)
        self.assertTrue(match)
        import html as html_mod

        self.client.post(reverse('walkthrough_import_confirm'),
                         {'payload': html_mod.unescape(match.group(1)), 'overwrite': '0'})
        obj = PaymentWalkthrough.objects.get(test_no='T-WT-001')
        self.assertEqual(len(obj.samples), 2)
        # 关键控制点 s2 未执行 → 结论不得高于「部分有效」，Excel 里的「有效」被忽略
        self.assertEqual(obj.conclusion, '部分有效')

    def test_template_has_two_sheets(self):
        resp = self.client.get(reverse('walkthrough_template'))
        wb = load_workbook(io.BytesIO(resp.content))
        self.assertEqual(wb.sheetnames[:2], ['测试清单', '样本明细'])


class ReviewAttachmentTests(ReviewIoTestBase):
    def test_attachment_upload_and_cascade_delete(self):
        obj = self.make_credit()
        upload = SimpleUploadedFile('报表.txt', b'balance sheet', content_type='text/plain')
        resp = self.client.post(
            f'/attachments/upload/records/customerreview/{obj.pk}/',
            {'files': [upload], 'category': 'finance'},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['ok'])
        att = Attachment.objects.get(object_id=obj.pk)
        self.assertEqual(att.category, 'finance')

        resp = self.client.get(reverse('customer_review_detail', args=[obj.pk]))
        self.assertContains(resp, '附件资料')

        self.client.post(reverse('customer_review_delete', args=[obj.pk]))
        self.assertFalse(Attachment.objects.filter(pk=att.pk).exists())

    def test_walkthrough_registered_for_attachments(self):
        from attachments.services import module_key_for

        obj = PaymentWalkthrough.objects.create(
            payee_name='测试供应商', period_start='2026-01-01',
            period_end='2026-06-30', test_date='2026-11-01',
        )
        self.assertEqual(module_key_for(obj), 'customer_review')


class WalkthroughTermsTests(ReviewIoTestBase):
    """账期执行核对：到期日推算、宽限期、结论压制、导出列。

    这一层最容易回归的是「口径漂移」——前端、服务端、导出各算一套。
    因此测试直接锚定 credit_spec 的常量与函数，不复制阈值。
    """

    def _sample(self, **kwargs):
        data = {'seq': 1, 'voucher_no': 'PAY-1', 'pay_date': '', 'terms_days': None,
                'base_date': '', 'due_date': '', 'steps': {}}
        data.update(kwargs)
        return data

    def test_due_date_falls_back_to_header_terms(self):
        info = spec.sample_terms(self._sample(base_date='2026-01-10'), 60)
        self.assertEqual(info['due_date'].isoformat(), '2026-03-11')
        self.assertEqual(info['terms_days'], 60)

    def test_sample_terms_override_header(self):
        info = spec.sample_terms(
            self._sample(base_date='2026-01-10', terms_days=30, due_date='2026-02-09'), 60
        )
        self.assertEqual(info['terms_days'], 30)
        self.assertEqual(info['due_date'].isoformat(), '2026-02-09')

    def test_grace_days_counted_as_on_time(self):
        grace = spec.TERMS_GRACE_DAYS
        due = '2026-03-11'
        info = spec.sample_terms(
            self._sample(base_date='2026-01-10', due_date=due,
                         pay_date=f'2026-03-{11 + grace:02d}'), 60
        )
        self.assertEqual(info['status'], '按期')
        over = spec.sample_terms(
            self._sample(base_date='2026-01-10', due_date=due,
                         pay_date=f'2026-03-{12 + grace:02d}'), 60
        )
        self.assertEqual(over['status'], '逾期')

    def test_no_dates_means_unchecked(self):
        info = spec.sample_terms(self._sample(pay_date='2026-03-01'), 60)
        self.assertIsNone(info['delay_days'])
        self.assertEqual(info['status'], '未核对')
        self.assertEqual(spec.terms_stats([self._sample(pay_date='2026-03-01')], 60)['checked'], 0)

    def test_bad_ratio_caps_conclusion(self):
        samples = [
            self._sample(seq=1, base_date='2026-01-01', pay_date='2026-06-01'),  # 逾期 151 天
            self._sample(seq=2, base_date='2026-01-01', pay_date='2026-06-01'),
        ]
        stats = spec.walkthrough_stats(samples, 30)
        self.assertTrue(stats['terms']['major'])
        self.assertEqual(stats['conclusion'], '部分有效')
        self.assertIn('账期执行偏差', stats['reason'])

    def test_single_small_overdue_keeps_mostly_effective(self):
        """个别小额逾期（未超 30 天、占比未过半）只降一档，不压到部分有效。"""
        samples = [
            self._sample(seq=1, base_date='2026-01-01', pay_date='2026-02-05'),  # 逾期约 5 天
            self._sample(seq=2, base_date='2026-01-01', pay_date='2026-01-31'),  # 按期
        ]
        stats = spec.walkthrough_stats(samples, 30)
        self.assertEqual(stats['terms']['overdue'], 1)
        self.assertEqual(stats['conclusion'], '基本有效')
        self.assertIn('逾期付款', stats['reason'])

    def test_major_overdue_caps_conclusion(self):
        """单笔逾期超过 30 天视为重大偏差，直接压到部分有效。"""
        samples = [
            self._sample(seq=1, base_date='2026-01-01', pay_date='2026-03-20'),  # 逾期约 48 天
            self._sample(seq=2, base_date='2026-01-01', pay_date='2026-01-31'),
        ]
        stats = spec.walkthrough_stats(samples, 30)
        self.assertTrue(stats['terms']['major'])
        self.assertEqual(stats['conclusion'], '部分有效')

    def test_view_saves_terms_and_recomputes_conclusion(self):
        import json

        payload = [
            {'seq': 1, 'voucher_no': 'PAY-1', 'amount': '100', 'payee': '供应商A',
             'terms_days': '', 'base_date': '2026-01-01', 'due_date': '', 'pay_date': '2026-06-01',
             'steps': {s['key']: {'result': '执行', 'defect': '无'} for s in spec.WALKTHROUGH_STEPS}},
        ]
        resp = self.client.post(reverse('walkthrough_add'), {
            'payee_name': '账期测试供应商', 'payee_type': '供应商',
            'period_start': '2026-01-01', 'period_end': '2026-06-30',
            'test_date': '2026-11-04', 'terms_text': '月结 30 天', 'terms_days': '30',
            'terms_base': '验收日', 'conclusion': '有效',
            'samples_json': json.dumps(payload, ensure_ascii=False),
        })
        self.assertEqual(resp.status_code, 302)
        obj = PaymentWalkthrough.objects.get(payee_name='账期测试供应商')
        self.assertEqual(obj.terms_days, 30)
        self.assertEqual(obj.samples[0]['base_date'], '2026-01-01')
        # Excel/表单里填的「有效」不生效：单笔逾期超 30 天 → 部分有效
        self.assertEqual(obj.conclusion, '部分有效')

    def test_export_sample_sheet_has_terms_columns(self):
        obj = PaymentWalkthrough.objects.create(
            test_no='T-WT-TERMS', payee_name='导出测试供应商', period_start='2026-01-01',
            period_end='2026-06-30', test_date='2026-11-01', terms_days=30,
            samples=[self._sample(base_date='2026-01-01', pay_date='2026-03-15')],
        )
        resp = self.client.get(reverse('walkthrough_export_one', args=[obj.pk]))
        wb = load_workbook(io.BytesIO(resp.content))
        ws = wb['样本明细']
        headers = [c.value for c in ws[1]]
        for col in ('约定账期天数', '账期起算日', '到期日', '实际付款日期', '偏差天数', '账期执行'):
            self.assertIn(col, headers)
        first = dict(zip(headers, [c.value for c in ws[2]]))
        self.assertEqual(first['到期日'], '2026-01-31')
        self.assertEqual(first['偏差天数'], 43)
        self.assertEqual(first['账期执行'], '逾期')
