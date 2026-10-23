"""档案总览查询数回归测试（审计 P2-2）。

修复前每个台账 4 条 SQL（count / 取行 / 即将到期 / 借出中），10 个台账实测 61 条。
改为「一次 annotate 取回全部计数 + 一条窗口函数取回所有台账首屏 10 行」后，
查询数应与台账数量、档案数量基本脱钩。

同时钉死行为不变：默认视图每台账仍只显示 10 行，计数语义不变。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import Archive, BorrowRecord, Ledger

LEDGER_COUNT = 8
ARCHIVES_PER_LEDGER = 6
BIG_LEDGER_ARCHIVES = 25


class OverviewQueryCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser("ov_root", "o@e.com", "X12345678")
        today = timezone.localdate()
        for li in range(LEDGER_COUNT):
            ledger = Ledger.objects.create(name=f"台账{li}")
            for ai in range(ARCHIVES_PER_LEDGER):
                arc = Archive.objects.create(
                    code=f"A{li}-{ai}", name=f"档案{li}-{ai}",
                    ledger=ledger, creator=cls.user,
                    # 每个台账第 0 条 10 天后到期（落入 30 天预警窗口）
                    expire_date=today + timedelta(days=10 if ai == 0 else 400),
                )
                if ai == 1:
                    BorrowRecord.objects.create(
                        archive=arc, borrower="借阅人",
                        due_time=today + timedelta(days=5),
                    )
        # 一个档案数远超 10 的台账：验证首屏仍只取 10 行
        big = Ledger.objects.create(name="大台账")
        for i in range(BIG_LEDGER_ARCHIVES):
            Archive.objects.create(
                code=f"BIG-{i:03d}", name=f"大档案{i}", ledger=big, creator=cls.user,
            )

    def setUp(self):
        self.client.force_login(self.user)

    def test_overview_query_count_is_bounded(self):
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(reverse("archive:overview"))
        self.assertEqual(resp.status_code, 200)
        # 会话/用户 3 条 + 台账 annotate 1 + 窗口函数取行 1 + 未分配聚合 1 + 仪表盘 3
        self.assertLessEqual(
            len(ctx.captured_queries), 14,
            f"总览页仍有 N+1：{len(ctx.captured_queries)} 条 SQL",
        )

    def test_overview_rows_capped_at_ten_per_ledger(self):
        resp = self.client.get(reverse("archive:overview"))
        rows_by_ledger = {
            d["ledger"].name: len(d["rows"]) for d in resp.context["ledger_data"]
        }
        self.assertEqual(rows_by_ledger["大台账"], 10)
        for li in range(LEDGER_COUNT):
            self.assertEqual(rows_by_ledger[f"台账{li}"], ARCHIVES_PER_LEDGER)

    def test_counts_semantics_preserved(self):
        """annotate 后的计数与逐台账查询结果一致（总数/即将到期/借出中）。"""
        resp = self.client.get(reverse("archive:overview"))
        data = {d["ledger"].name: d for d in resp.context["ledger_data"]}
        self.assertEqual(data["台账0"]["total"], ARCHIVES_PER_LEDGER)
        self.assertEqual(data["台账0"]["expiring_count"], 1)
        self.assertEqual(data["台账0"]["borrowed_count"], 1)
        # 仪表盘：全站即将到期 = 每个台账 1 条
        self.assertEqual(resp.context["expiring_count"], LEDGER_COUNT)
        self.assertEqual(resp.context["borrowed_count"], LEDGER_COUNT)

    def test_unassigned_archives_shown(self):
        Archive.objects.create(code="U001", name="未分配档案", ledger=None, creator=self.user)
        resp = self.client.get(reverse("archive:overview"))
        self.assertEqual(resp.context["unassigned_total"], 1)
        self.assertEqual(len(resp.context["unassigned_rows"]), 1)

    def test_search_filter_still_works(self):
        """搜索命中档案所在台账才展示（搜索词匹配档案字段，不是台账名）。"""
        resp = self.client.get(reverse("archive:overview"), {"aq": "档案3-1"})
        self.assertEqual(resp.status_code, 200)
        names = [d["ledger"].name for d in resp.context["ledger_data"]]
        self.assertEqual(names, ["台账3"])
        # 未命中任何档案时不展示任何台账卡片
        resp = self.client.get(reverse("archive:overview"), {"aq": "不存在的关键词"})
        self.assertEqual([d["ledger"].name for d in resp.context["ledger_data"]], [])
