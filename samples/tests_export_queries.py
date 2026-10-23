"""样品导出查询数回归测试（审计 P2-1）。

修复前每导出一行样品要 5 条 SQL（试样 .first() / 小批量 .first() / 小批量 .all() /
两个各自查库的 model property），20 条样品共 110 条，导出量一大在 SQLite 上直接卡死。

这里同时钉死两件事：
1. 查询数与样品数无关（预取后恒为个位数）；
2. 导出内容语义不变——"最新轮次"仍是 Meta.ordering 下的最大轮次，不是乱序取首行。
"""

import io

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from openpyxl import load_workbook

from .exporters import export_samples_xlsx
from .models import BatchRecord, Sample, TestRecord

SAMPLE_COUNT = 12


def _make_sample(idx):
    s = Sample.objects.create(
        sample_no=f"S-T-{idx:03d}",
        material_name=f"物料{idx}",
        model=f"M-{idx}",
        spec="spec",
        incoming_qty=idx,
        sample_date=timezone.localdate(),
        status=Sample.Status.TESTING,
    )
    for rnd in (1, 2):
        TestRecord.objects.create(
            test_no=f"T-{idx}-{rnd}",
            sample=s,
            round_no=rnd,
            test_date=timezone.localdate(),
            test_result=f"结果{rnd}",
            review_conclusion="pass",
            report_code=f"RC-{idx}-{rnd}",
        )
        BatchRecord.objects.create(
            batch_no=f"B-{idx}-{rnd}",
            sample=s,
            round_no=rnd,
            batch_result="pass",
            kingdee_entered=(rnd == 2),
        )
    return s


class SampleExportQueryCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for i in range(SAMPLE_COUNT):
            _make_sample(i)

    def test_export_queries_are_bounded(self):
        """12 条样品 × 每行 2 试样 + 2 小批量：查询数必须是个位数。"""
        with CaptureQueriesContext(connection) as ctx:
            export_samples_xlsx(Sample.objects.all())
        # 1 次样品 + 1 次供应商 + 1 次 test 预取 + 1 次 batch 预取 + 2 个 sheet 的 join
        self.assertLessEqual(
            len(ctx.captured_queries), 8,
            f"导出仍有 N+1：{len(ctx.captured_queries)} 条 SQL（应为个位数）",
        )

    def test_latest_round_semantics_preserved(self):
        """导出内容必须是最大轮次：试产轮次列=2、最新结论/金蝶标记取第 2 轮。"""
        wb = load_workbook(io.BytesIO(export_samples_xlsx(Sample.objects.all()).content))
        ws = wb["样品主档"]
        rows = list(ws.iter_rows(min_row=2, values_only=True))
        self.assertEqual(len(rows), SAMPLE_COUNT)
        for row in rows:
            (
                sample_no, _material, _model, _spec, _qty, _supplier, _sample_date,
                _status, _registrar, _registered_at,
                test_round, test_conclusion, batch_round, batch_result, kingdee,
                _terminate,
            ) = row
            self.assertEqual(test_round, 2, f"{sample_no} 试产轮次应取最大轮次")
            self.assertEqual(test_conclusion, "通过")
            self.assertEqual(batch_round, 2)
            self.assertEqual(batch_result, "通过")
            self.assertEqual(kingdee, "是", "第 2 轮录入了金蝶")

    def test_kingdee_flag_scans_all_batches(self):
        """是否已录入金蝶遍历全部小批量记录（预取后仍走全量缓存，不查库）。"""
        sample = Sample.objects.get(sample_no="S-T-001")
        BatchRecord.objects.filter(sample=sample, round_no=1).update(kingdee_entered=False)
        BatchRecord.objects.filter(sample=sample, round_no=2).update(kingdee_entered=True)
        wb = load_workbook(io.BytesIO(export_samples_xlsx(Sample.objects.filter(pk=sample.pk)).content))
        ws = wb["样品主档"]
        row = list(ws.iter_rows(min_row=2, values_only=True))[0]
        self.assertEqual(row[14], "是")
