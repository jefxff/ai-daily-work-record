"""档案管理模块核心单元测试。

覆盖：模型属性计算、表单校验（位置越界/占用）、权限控制（仅超管可见）、
档案 CRUD 与借阅流程。
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .forms import ArchiveForm, CabinetForm
from .models import Archive, BorrowRecord, Cabinet, Category

User = get_user_model()


class CabinetModelTests(TestCase):
    """档案柜模型属性测试。"""

    def setUp(self):
        self.cab = Cabinet.objects.create(code="A01", location="3楼", layers=4, cells_per_layer=10)

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


class ArchiveModelTests(TestCase):
    """档案模型属性测试。"""

    def setUp(self):
        self.cab = Cabinet.objects.create(code="A01", layers=4, cells_per_layer=10)
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
        self.cab = Cabinet.objects.create(code="A01", layers=4, cells_per_layer=10)

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
        self.cab = Cabinet.objects.create(code="A01", layers=2, cells_per_layer=2)
        self.arc = Archive.objects.create(
            code="T001", name="测试", cabinet=self.cab, layer=1, cell=1, creator=self.user
        )

    def test_borrow_then_return(self):
        self.client.force_login(self.user)
        # 借阅
        resp = self.client.post(
            reverse("archive:borrow_create", args=[self.arc.pk]),
            {"borrower": self.user.pk, "due_time": "2099-01-01"},
        )
        self.assertEqual(resp.status_code, 302)
        self.arc.refresh_from_db()
        self.assertTrue(self.arc.is_borrowed)
        rec = BorrowRecord.objects.first()
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
            "code": "B02", "location": "2楼", "layers": 6, "cells_per_layer": 8,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Cabinet.objects.filter(code="B02").exists())

    def test_archive_create_vacancy(self):
        cab = Cabinet.objects.create(code="C03", layers=2, cells_per_layer=2)
        resp = self.client.post(reverse("archive:archive_create"), {
            "code": "D001", "name": "档案1", "cabinet": cab.pk, "layer": 1, "cell": 1,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Archive.objects.filter(code="D001").exists())

    def test_cabinet_delete_with_archive_blocked(self):
        cab = Cabinet.objects.create(code="D04", layers=2, cells_per_layer=2)
        Archive.objects.create(code="X", name="x", cabinet=cab, layer=1, cell=1)
        resp = self.client.post(reverse("archive:cabinet_delete", args=[cab.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Cabinet.objects.filter(pk=cab.pk).exists())


class CabinetFormTests(TestCase):
    def test_valid_form(self):
        form = CabinetForm(data={"code": "Z9", "location": "x", "layers": 4, "cells_per_layer": 5})
        self.assertTrue(form.is_valid())
