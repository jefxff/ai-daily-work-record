"""档案管理模块核心单元测试。

覆盖：模型属性计算、表单校验（位置越界/占用）、权限控制（仅超管可见）、
档案 CRUD 与借阅流程。
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .forms import ArchiveForm, CabinetForm, LedgerForm
from .models import Archive, BorrowRecord, Cabinet, Category, Ledger, License

User = get_user_model()


def make_cabinet(code, layers, cells, location=""):
    """测试辅助：创建档案柜并按 cells（整数=统一格数，列表=逐层格数）生成层记录。"""
    cab = Cabinet.objects.create(code=code, location=location, layers=layers)
    if isinstance(cells, int):
        cells = [cells] * layers
    for no, c in enumerate(cells, start=1):
        cab.layer_configs.create(layer_no=no, cells=c)
    return cab


class CabinetModelTests(TestCase):
    """档案柜模型属性测试。"""

    def setUp(self):
        self.cab = make_cabinet("A01", 4, 10, location="3楼")

    def test_total_cells(self):
        self.assertEqual(self.cab.total_cells, 40)

    def test_used_and_free_cells(self):
        self.assertEqual(self.cab.used_cells, 0)
        self.assertEqual(self.cab.free_cells, 40)
        Archive.objects.create(
            code="T001", name="测试档案", cabinet=self.cab, layer=1, cell=1
        )
        self.assertEqual(self.cab.used_cells, 1)
        self.assertEqual(self.cab.free_cells, 39)

    def test_occupancy_rate(self):
        self.assertEqual(self.cab.occupancy_rate, 0.0)
        Archive.objects.create(code="T001", name="t", cabinet=self.cab, layer=1, cell=1)
        self.assertAlmostEqual(self.cab.occupancy_rate, 2.5)

    def test_cell_matrix_dimensions(self):
        matrix = self.cab.cell_matrix()
        self.assertEqual(len(matrix), 4)
        self.assertEqual(len(matrix[0]), 10)

    def test_per_layer_cells(self):
        """每层格数可独立设置：总格子 = 各层格数之和。"""
        cab = make_cabinet("B02", 3, [10, 8, 6])
        self.assertEqual(cab.cells_of_layer(1), 10)
        self.assertEqual(cab.cells_of_layer(2), 8)
        self.assertEqual(cab.cells_of_layer(3), 6)
        self.assertEqual(cab.total_cells, 24)
        self.assertEqual(cab.layer_cells_summary(), "10,8,6")
        matrix = cab.cell_matrix()
        self.assertEqual([len(r) for r in matrix], [10, 8, 6])

    def test_sync_layers_grow_and_shrink(self):
        """sync_layers：增层补记录、缩层删记录、逐层改格数。"""
        cab = make_cabinet("C03", 2, [10, 8])
        cab.layers = 4
        cab.sync_layers([5, 6, 7, 8])
        self.assertEqual(cab.layer_cells_summary(), "5,6,7,8")
        cab.layers = 2
        cab.sync_layers([5, 6])
        self.assertEqual(cab.layer_cells_summary(), "5,6")

    def test_sync_layers_shrink_blocked_by_archives(self):
        """缩层时被删层上有档案 → 拒绝。"""
        cab = make_cabinet("D04", 3, 4)
        Archive.objects.create(code="T100", name="t", cabinet=cab, layer=3, cell=1)
        cab.layers = 2
        with self.assertRaises(ValueError):
            cab.sync_layers()

    def test_sync_layers_shrink_cells_blocked_by_archives(self):
        """缩格时该层格号超过新格数 → 拒绝。"""
        cab = make_cabinet("E05", 2, 6)
        Archive.objects.create(code="T200", name="t", cabinet=cab, layer=1, cell=5)
        with self.assertRaises(ValueError):
            cab.sync_layers([3, 6])


class ArchiveModelTests(TestCase):
    """档案模型属性测试。"""

    def setUp(self):
        self.cab = make_cabinet("A01", 4, 10)
        self.arc = Archive.objects.create(
            code="T001", name="测试档案", cabinet=self.cab, layer=2, cell=3, tags="标签1, 标签2"
        )

    def test_location_path(self):
        self.assertEqual(self.arc.location_path, "A01-2层-3格")

    def test_tags_list(self):
        self.assertEqual(self.arc.tags_list, ["标签1", "标签2"])

    def test_is_borrowed_false(self):
        self.assertFalse(self.arc.is_borrowed)


class CategoryModelTests(TestCase):
    """类别树形路径测试。"""

    def test_full_path(self):
        parent = Category.objects.create(name="客户档案")
        child = Category.objects.create(name="客诉记录", parent=parent)
        self.assertEqual(child.full_path, "客户档案/客诉记录")


class ArchiveFormTests(TestCase):
    """档案表单校验测试。"""

    def setUp(self):
        self.cab = make_cabinet("A01", 4, 10)

    def test_layer_out_of_range(self):
        form = ArchiveForm(data={
            "code": "X", "name": "x", "cabinet": self.cab.pk, "layer": 5, "cell": 1,
        })
        self.assertFalse(form.is_valid())
        self.assertIn("layer", form.errors)

    def test_cell_out_of_range(self):
        form = ArchiveForm(data={
            "code": "X", "name": "x", "cabinet": self.cab.pk, "layer": 1, "cell": 11,
        })
        self.assertFalse(form.is_valid())
        self.assertIn("cell", form.errors)

    def test_duplicate_position(self):
        Archive.objects.create(code="T001", name="t", cabinet=self.cab, layer=1, cell=1)
        form = ArchiveForm(data={
            "code": "T002", "name": "t2", "cabinet": self.cab.pk, "layer": 1, "cell": 1,
        })
        self.assertFalse(form.is_valid())
        self.assertTrue(form.non_field_errors())


class PermissionTests(TestCase):
    """权限控制测试：验收前仅超管可见。"""

    def setUp(self):
        self.superuser = User.objects.create_superuser("admin", "a@a.com", "pass")
        self.staff = User.objects.create_user("staff", "s@s.com", "pass")

    def test_overview_superuser_200(self):
        self.client.force_login(self.superuser)
        resp = self.client.get(reverse("archive:overview"))
        self.assertEqual(resp.status_code, 200)

    def test_overview_staff_403(self):
        # 普通用户无 archive 权限（数据库中无 archive 权限码）
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("archive:overview"))
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_redirect(self):
        resp = self.client.get(reverse("archive:overview"))
        self.assertEqual(resp.status_code, 302)


class BorrowFlowTests(TestCase):
    """借阅流程测试。"""

    def setUp(self):
        self.user = User.objects.create_superuser("admin", "a@a.com", "pass")
        self.cab = make_cabinet("A01", 2, 2)
        self.arc = Archive.objects.create(
            code="T001", name="测试", cabinet=self.cab, layer=1, cell=1, creator=self.user
        )

    def test_borrow_then_return(self):
        self.client.force_login(self.user)
        # 借阅（借阅人为自由输入的姓名）
        resp = self.client.post(
            reverse("archive:borrow_create", args=[self.arc.pk]),
            {"borrower": "张三", "due_time": "2099-01-01"},
        )
        self.assertEqual(resp.status_code, 302)
        self.arc.refresh_from_db()
        self.assertTrue(self.arc.is_borrowed)
        rec = BorrowRecord.objects.first()
        self.assertEqual(rec.borrower, "张三")
        # 归还
        resp = self.client.post(reverse("archive:borrow_return", args=[rec.pk]))
        self.assertEqual(resp.status_code, 302)
        rec.refresh_from_db()
        self.assertEqual(rec.status, BorrowRecord.Status.RETURNED)
        self.arc.refresh_from_db()
        self.assertFalse(self.arc.is_borrowed)


class CRUDFlowTests(TestCase):
    """档案柜与档案 CRUD 流程测试。"""

    def setUp(self):
        self.user = User.objects.create_superuser("admin", "a@a.com", "pass")
        self.client.force_login(self.user)

    def test_cabinet_create(self):
        resp = self.client.post(reverse("archive:cabinet_create"), {
            "code": "B02", "location": "2楼", "layers": 3,
            "layer_cell_1": "8", "layer_cell_2": "6", "layer_cell_3": "4",
        })
        self.assertEqual(resp.status_code, 302)
        cab = Cabinet.objects.get(code="B02")
        self.assertEqual(cab.layer_cells_summary(), "8,6,4")
        self.assertEqual(cab.total_cells, 18)

    def test_archive_create_vacancy(self):
        cab = make_cabinet("C03", 2, 2)
        resp = self.client.post(reverse("archive:archive_create"), {
            "code": "D001", "name": "档案1", "cabinet": cab.pk, "layer": 1, "cell": 1,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Archive.objects.filter(code="D001").exists())

    def test_cabinet_delete_with_archive_blocked(self):
        cab = make_cabinet("D04", 2, 2)
        Archive.objects.create(code="X", name="x", cabinet=cab, layer=1, cell=1)
        resp = self.client.post(reverse("archive:cabinet_delete", args=[cab.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Cabinet.objects.filter(pk=cab.pk).exists())


class CabinetFormTests(TestCase):
    def test_valid_form(self):
        form = CabinetForm(data={"code": "Z9", "location": "x", "layers": 4})
        self.assertTrue(form.is_valid())


class LedgerFormTests(TestCase):
    """台账表单校验。"""

    def test_valid_form(self):
        form = LedgerForm(data={"name": "专利合同台账", "description": "专利相关合同", "sort_order": 1})
        self.assertTrue(form.is_valid())

    def test_name_required(self):
        form = LedgerForm(data={"name": ""})
        self.assertFalse(form.is_valid())


class LedgerFlowTests(TestCase):
    """台账流程：导入归属台账、总览渲染台账卡片、详情分页、非空台账禁止删除。"""

    def setUp(self):
        self.user = User.objects.create_superuser("admin", "a@a.com", "pass")
        self.client.force_login(self.user)

    @staticmethod
    def _xlsx_bytes():
        import io

        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "合同"
        ws.append(["合同名称", "相对方", "合同编号"])
        ws.append(["专利许可合同", "德立科技", "HT-001"])
        ws.append(["商标代理合同", "德众科技", "HT-002"])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return buf.read()

    def test_import_assigns_ledger(self):
        """导入 Excel 时档案自动归属台账并记录来源 sheet。"""
        from django.core.files.uploadedfile import SimpleUploadedFile

        f = SimpleUploadedFile(
            "专利、商标相关合同.xlsx", self._xlsx_bytes(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp = self.client.post(reverse("archive:import_archives"), {"step": "upload", "file": f})
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(reverse("archive:import_archives"), {
            "step": "import",
            "ledger_name": "专利合同台账",
            "map__合同__合同名称": "name",
            "map__合同__相对方": "counterpart",
            "map__合同__合同编号": "contract_no",
        })
        self.assertEqual(resp.status_code, 302)
        ledger = Ledger.objects.get(name="专利合同台账")
        self.assertEqual(ledger.archives.count(), 2)
        arc = ledger.archives.first()
        self.assertEqual(arc.source_sheet, "合同")
        self.assertEqual(
            set(ledger.archives.values_list("counterpart", flat=True)), {"德立科技", "德众科技"}
        )

    def test_overview_renders_ledger_cards(self):
        """总览页按台账渲染卡片，显示台账名与档案预览。"""
        ledger = Ledger.objects.create(name="证照台账")
        Archive.objects.create(code="Z001", name="营业执照", ledger=ledger, creator=self.user)
        resp = self.client.get(reverse("archive:overview"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "证照台账")
        self.assertContains(resp, "Z001")

    def test_ledger_detail_pagination(self):
        """台账详情分页：per_page=10 时第二页仅剩 2 条。"""
        ledger = Ledger.objects.create(name="分页台账")
        for i in range(12):
            Archive.objects.create(code=f"P{i:03d}", name=f"档案{i}", ledger=ledger, creator=self.user)
        resp = self.client.get(reverse("archive:ledger_detail", args=[ledger.pk]), {"per_page": 10, "page": 2})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["page_obj"]), 2)
        self.assertEqual(resp.context["page_obj"].paginator.count, 12)

    def test_ledger_delete_cascades_archives(self):
        """台账删除为级联删除：连同其下档案一并删除（需求变更：移除存在档案时的删除拦截）。"""
        ledger = Ledger.objects.create(name="非空台账")
        arc = Archive.objects.create(code="D001", name="占用档案", ledger=ledger, creator=self.user)
        resp = self.client.post(reverse("archive:ledger_delete", args=[ledger.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Ledger.objects.filter(pk=ledger.pk).exists())
        self.assertFalse(Archive.objects.filter(pk=arc.pk).exists())
        empty = Ledger.objects.create(name="空台账")
        resp = self.client.post(reverse("archive:ledger_delete", args=[empty.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Ledger.objects.filter(pk=empty.pk).exists())


class TableImportExportTests(TestCase):
    """表级通用导入/导出：全表导出、round-trip 更新、新增、权限控制。"""

    ALL_TABLES = [
        "archives", "ledgers", "cabinets", "categories",
        "borrows", "licenses", "reviews", "license_borrows",
    ]

    def setUp(self):
        self.user = User.objects.create_superuser("admin", "a@a.com", "pass")
        self.client.force_login(self.user)
        self.ledger = Ledger.objects.create(name="测试台账")
        self.arc = Archive.objects.create(
            code="T001", name="测试档案", ledger=self.ledger, counterpart="甲公司",
            creator=self.user,
        )

    @staticmethod
    def _xlsx(rows, headers):
        import io

        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(headers)
        for r in rows:
            ws.append(r)
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return buf.read()

    def _upload(self, table, rows, headers):
        from django.core.files.uploadedfile import SimpleUploadedFile

        f = SimpleUploadedFile(
            "data.xlsx", self._xlsx(rows, headers),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp = self.client.post(reverse("archive:table_import", args=[table]), {"step": "upload", "file": f})
        self.assertEqual(resp.status_code, 200)
        return resp

    def _commit(self, table):
        return self.client.post(reverse("archive:table_import", args=[table]), {"step": "commit"})

    def test_export_all_tables_200(self):
        for table in self.ALL_TABLES:
            with self.subTest(table=table):
                resp = self.client.get(reverse("archive:table_export", args=[table]))
                self.assertEqual(resp.status_code, 200)
                self.assertIn("spreadsheetml", resp["Content-Type"])

    def test_legacy_csv_export_no_500(self):
        """旧导出入口（无档案柜档案）不再 500。"""
        resp = self.client.get(reverse("archive:export"))
        self.assertEqual(resp.status_code, 200)

    def test_unknown_table_404(self):
        resp = self.client.get(reverse("archive:table_export", args=["nope"]))
        self.assertEqual(resp.status_code, 404)

    def test_anonymous_redirect(self):
        self.client.logout()
        resp = self.client.get(reverse("archive:table_export", args=["archives"]))
        self.assertEqual(resp.status_code, 302)

    def test_roundtrip_update_archive(self):
        """导出→原样导入→全部更新且字段不变；改名后导入→字段确实更新。"""
        Archive.objects.create(code="T002", name="第二档案", ledger=self.ledger, creator=self.user)
        # round-trip：导出内容原样回传
        resp = self.client.get(reverse("archive:table_export", args=["archives"]))
        import io

        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(resp.content))
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        headers = [str(h) for h in rows[0]]
        data = [list(r) for r in rows[1:]]
        self._upload("archives", data, headers)
        resp = self._commit("archives")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Archive.objects.count(), 2)
        self.arc.refresh_from_db()
        self.assertEqual(self.arc.name, "测试档案")
        self.assertEqual(self.arc.counterpart, "甲公司")
        # 修改一行名称后导入 → 更新
        data[0][headers.index("名称")] = "改名档案"
        data[0][headers.index("公司名称/相对方")] = "乙公司"
        self._upload("archives", data, headers)
        resp = self._commit("archives")
        self.assertEqual(resp.status_code, 302)
        self.arc.refresh_from_db()
        self.assertEqual(self.arc.name, "改名档案")
        self.assertEqual(self.arc.counterpart, "乙公司")
        self.assertEqual(Archive.objects.count(), 2)

    def test_import_creates_new_archive(self):
        """匹配键不存在时新增档案（自动创建所属台账）。"""
        self._upload(
            "archives",
            [["T100", "新档案", "新建台账", "", "", "", "", "", "", "丙公司"]],
            ["编号", "名称", "所属台账", "类别", "档案柜", "层", "格", "位置编码", "来源Sheet", "公司名称/相对方"],
        )
        resp = self._commit("archives")
        self.assertEqual(resp.status_code, 302)
        arc = Archive.objects.filter(code="T100").first()
        self.assertIsNotNone(arc)
        self.assertEqual(arc.ledger.name, "新建台账")
        self.assertEqual(arc.counterpart, "丙公司")

    def test_import_ledger_update(self):
        """台账表按名称匹配更新说明字段。"""
        self._upload(
            "ledgers",
            [["测试台账", "更新后的说明", 5]],
            ["台账名称", "说明", "排序"],
        )
        resp = self._commit("ledgers")
        self.assertEqual(resp.status_code, 302)
        self.ledger.refresh_from_db()
        self.assertEqual(self.ledger.description, "更新后的说明")
        self.assertEqual(self.ledger.sort_order, 5)
        self.assertEqual(Ledger.objects.count(), 1)

    def test_import_borrow_roundtrip_and_status(self):
        """借阅记录按记录ID更新：写入归还时间后状态自动重算为已归还。"""
        from django.utils import timezone

        br = BorrowRecord.objects.create(
            archive=self.arc, borrower="张三",
            borrow_time=timezone.now(), due_time="2099-01-01",
        )
        self._upload(
            "borrows",
            [[br.pk, "T001", "张三", "2026-01-01 10:00", "2099-01-01", "2026-02-01 09:00", "批量归还备注"]],
            ["记录ID", "档案编号", "借阅人", "借阅时间", "应归还日期", "实际归还时间", "备注"],
        )
        resp = self._commit("borrows")
        self.assertEqual(resp.status_code, 302)
        br.refresh_from_db()
        self.assertIsNotNone(br.return_time)
        self.assertEqual(br.status, BorrowRecord.Status.RETURNED)

    def test_import_missing_key_row_reported(self):
        """匹配键为空的行报错跳过，不中断其他行。"""
        self._upload(
            "ledgers",
            [["测试台账", "ok", 1], ["", "bad", 2]],
            ["台账名称", "说明", "排序"],
        )
        resp = self._commit("ledgers")
        self.assertEqual(resp.status_code, 302)
        self.ledger.refresh_from_db()
        self.assertEqual(self.ledger.description, "ok")
        self.assertEqual(Ledger.objects.count(), 1)


ARCHIVE_HEADERS = [
    "编号", "名称", "所属台账", "类别", "档案柜", "层", "格", "位置编码", "来源Sheet",
    "公司名称/相对方", "合同编号", "金额", "签订日期", "到期日期", "部门", "保密等级",
    "档案标签", "经办人", "份数", "附件描述", "标签", "档案描述",
]


class LedgerImportExportTests(TestCase):
    """台账维度档案导入/导出：单台账 round-trip、勾选批量导出、多工作表批量导入。"""

    def setUp(self):
        self.user = User.objects.create_superuser("admin", "a@a.com", "pass")
        self.client.force_login(self.user)
        self.ledger = Ledger.objects.create(name="台账A")
        self.arc = Archive.objects.create(
            code="L001", name="档案一", ledger=self.ledger, counterpart="甲公司",
            creator=self.user,
        )

    @staticmethod
    def _xlsx(rows, headers):
        import io

        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(headers)
        for r in rows:
            ws.append(r)
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return buf.read()

    @staticmethod
    def _multi_sheet_xlsx(sheets):
        """sheets: [(sheet名, rows)] → xlsx bytes（共享 ARCHIVE_HEADERS 表头）。"""
        import io

        from openpyxl import Workbook

        wb = Workbook()
        wb.remove(wb.active)
        for name, rows in sheets:
            ws = wb.create_sheet(title=name[:31])
            ws.append(ARCHIVE_HEADERS)
            for r in rows:
                ws.append(r)
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return buf.read()

    def _upload_file(self, url, content):
        from django.core.files.uploadedfile import SimpleUploadedFile

        f = SimpleUploadedFile(
            "data.xlsx", content,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        return self.client.post(url, {"step": "upload", "file": f})

    def test_ledger_export_single_sheet(self):
        resp = self.client.get(reverse("archive:ledger_export", args=[self.ledger.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("spreadsheetml", resp["Content-Type"])
        import io

        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(resp.content))
        self.assertEqual(wb.sheetnames, ["台账A"])
        rows = list(wb.active.iter_rows(values_only=True))
        self.assertEqual(len(rows), 2)  # 表头 + 1 条档案
        self.assertEqual(rows[1][0], "L001")

    def test_ledger_roundtrip_update(self):
        """导出本台账 → 改名 → 导入更新 → 字段更新且归属不变。"""
        resp = self.client.get(reverse("archive:ledger_export", args=[self.ledger.pk]))
        import io

        from openpyxl import load_workbook

        rows = list(load_workbook(io.BytesIO(resp.content)).active.iter_rows(values_only=True))
        headers = [str(h) for h in rows[0]]
        data = [list(r) for r in rows[1:]]
        data[0][headers.index("名称")] = "改名档案"
        url = reverse("archive:ledger_import", args=[self.ledger.pk])
        resp = self._upload_file(url, self._xlsx(data, headers))
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(url, {"step": "commit"})
        self.assertEqual(resp.status_code, 302)
        self.arc.refresh_from_db()
        self.assertEqual(self.arc.name, "改名档案")
        self.assertEqual(self.arc.ledger, self.ledger)
        self.assertEqual(Archive.objects.count(), 1)

    def test_ledger_import_creates_in_ledger(self):
        """台账内导入新编号 → 新增档案归入本台账（忽略文件中的台账列）。"""
        data = [["L100", "新档案", "别的台账", "", "", "", "", "", "", "丙公司"] + [""] * 12]
        url = reverse("archive:ledger_import", args=[self.ledger.pk])
        self._upload_file(url, self._xlsx(data, ARCHIVE_HEADERS))
        resp = self.client.post(url, {"step": "commit"})
        self.assertEqual(resp.status_code, 302)
        arc = Archive.objects.filter(code="L100").first()
        self.assertIsNotNone(arc)
        self.assertEqual(arc.ledger, self.ledger)
        self.assertEqual(Ledger.objects.count(), 1)

    def test_batch_export_selected_sheets(self):
        lg2 = Ledger.objects.create(name="台账B")
        Archive.objects.create(code="L002", name="档案二", ledger=lg2, creator=self.user)
        resp = self.client.post(reverse("archive:ledger_batch_export"), {"ledgers": [self.ledger.pk, lg2.pk]})
        self.assertEqual(resp.status_code, 200)
        import io

        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(resp.content))
        self.assertEqual(wb.sheetnames, ["台账A", "台账B"])
        # 未勾选 → 提示回总览
        resp = self.client.post(reverse("archive:ledger_batch_export"), {})
        self.assertEqual(resp.status_code, 302)

    def test_batch_import_multi_sheet(self):
        """多工作表导入：已有台账更新 + 新台账自动创建。"""
        sheets = [
            ("台账A", [["L001", "改名档案"] + [""] * 20]),
            ("台账C", [["L300", "C档案", "", "", "", "", "", "", "", "丙公司"] + [""] * 12]),
        ]
        url = reverse("archive:ledger_batch_import")
        resp = self._upload_file(url, self._multi_sheet_xlsx(sheets))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "工作表")
        resp = self.client.post(url, {"step": "commit"})
        self.assertEqual(resp.status_code, 302)
        self.arc.refresh_from_db()
        self.assertEqual(self.arc.name, "改名档案")
        lg_c = Ledger.objects.filter(name="台账C").first()
        self.assertIsNotNone(lg_c)
        self.assertEqual(lg_c.archives.filter(code="L300", counterpart="丙公司").count(), 1)

    def test_batch_import_missing_code_column(self):
        url = reverse("archive:ledger_batch_import")
        import io

        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "坏表"
        ws.append(["名称", "档案一"])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        resp = self._upload_file(url, buf.read())
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Archive.objects.count(), 1)

    def test_anonymous_redirect(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("archive:ledger_export", args=[self.ledger.pk])).status_code, 302)
        self.assertEqual(self.client.get(reverse("archive:ledger_import", args=[self.ledger.pk])).status_code, 302)
        self.assertEqual(self.client.get(reverse("archive:ledger_batch_import")).status_code, 302)

    def test_pages_render_new_entries(self):
        resp = self.client.get(reverse("archive:overview"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "导出所选")
        self.assertContains(resp, "批量导入更新")
        self.assertContains(resp, "/archive/ledgers/export/")
        resp = self.client.get(reverse("archive:ledger_detail", args=[self.ledger.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "导出本台账")
        self.assertContains(resp, "导入更新")
        # 导入上传页可渲染
        resp = self.client.get(reverse("archive:ledger_import", args=[self.ledger.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "导入更新「台账A」")


class LicensePageTests(TestCase):
    """证照录入/编辑全页路径。

    审计 E2E 发现的基线缺陷：非 AJAX 直接访问 /archive/licenses/<pk>/edit/ 时
    视图回退渲染 archive/license_form.html，而该模板在基线里不存在，页面 500
    （只有 modal 局部模板）。这里固化全页渲染，防回归。
    """

    def setUp(self):
        self.user = User.objects.create_superuser("lic_admin", "l@a.com", "pass")
        self.lic = License.objects.create(name="营业执照", code="LIC-001", creator=self.user)

    def test_license_create_full_page(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("archive:license_create"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "证照名称")

    def test_license_edit_full_page(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("archive:license_edit", args=[self.lic.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.lic.code)
        self.assertContains(resp, "返回证照列表")
