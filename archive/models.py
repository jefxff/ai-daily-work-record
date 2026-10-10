"""档案管理模块数据模型。

模型说明：
- Cabinet       档案柜（编号、位置、层数、每层格数）
- Category      档案类别（树形多级结构）
- Archive       档案（编号、名称、柜/层/格定位、电子文件、类别、标签）
- BorrowRecord  借阅记录（借阅人、借出/应还/实还时间、状态）

操作日志复用 records.models.OperationLog，避免重复建设。
"""

import os

from django.conf import settings
from django.db import models
from django.utils import timezone


class TimeStampedModel(models.Model):
    """统一创建/更新时间基类。"""

    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        abstract = True


class Cabinet(TimeStampedModel):
    """档案柜：定义物理存放位置的层级结构（柜 -> 层 -> 格）。"""

    LAYER_CHOICES = [(2, "2 层"), (4, "4 层"), (6, "6 层"), (8, "8 层")]

    code = models.CharField("档案柜编号", max_length=50, unique=True)
    location = models.CharField("所在位置", max_length=200, blank=True)
    layers = models.PositiveSmallIntegerField("层数", choices=LAYER_CHOICES, default=4)
    cells_per_layer = models.PositiveSmallIntegerField("每层格数", default=10)

    class Meta:
        verbose_name = "档案柜"
        verbose_name_plural = "档案柜"
        ordering = ["code"]

    def __str__(self):
        return f"{self.code}（{self.location}）"

    @property
    def total_cells(self) -> int:
        """档案柜总格子数。"""
        return int(self.layers) * int(self.cells_per_layer)

    @property
    def used_cells(self) -> int:
        """已被档案占用的格子数（按柜+层+格去重计数）。"""
        return (
            self.archives.values_list("layer", "cell")
            .distinct()
            .count()
        )

    @property
    def free_cells(self) -> int:
        """剩余空位数量。"""
        return self.total_cells - self.used_cells

    @property
    def occupancy_rate(self) -> float:
        """占用率（百分比，0-100）。"""
        total = self.total_cells
        return round(self.used_cells * 100 / total, 1) if total else 0.0

    def cell_matrix(self):
        """返回二维矩阵：[层][格] -> Archive 或 None，用于总览页渲染。"""
        archives = {(a.layer, a.cell): a for a in self.archives.all()}
        matrix = []
        for layer in range(1, self.layers + 1):
            row = []
            for cell in range(1, self.cells_per_layer + 1):
                row.append(archives.get((layer, cell)))
            matrix.append(row)
        return matrix


class Category(models.Model):
    """档案类别（树形多级结构，支持父子关系）。"""

    name = models.CharField("类别名称", max_length=100)
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="children",
        verbose_name="父类别",
    )
    sort_order = models.IntegerField("排序", default=0)

    class Meta:
        verbose_name = "档案类别"
        verbose_name_plural = "档案类别"
        ordering = ["sort_order", "id"]

    def __str__(self):
        return self.name

    @property
    def full_path(self) -> str:
        """返回完整类别路径，如 '客户档案/客诉记录'。"""
        names = [self.name]
        node = self.parent
        while node is not None:
            names.insert(0, node.name)
            node = node.parent
        return "/".join(names)


def archive_file_upload_to(instance, filename):
    """档案文件上传路径：media/archives/<柜编号>/<文件名>。"""
    cabinet_code = instance.cabinet.code if instance.cabinet else "unknown"
    # 仅保留文件名，避免路径注入
    filename = os.path.basename(filename)
    return os.path.join("archives", cabinet_code, filename)


class Archive(TimeStampedModel):
    """档案：物理定位 + 电子文件 + 分类标签。"""

    code = models.CharField("档案编号", max_length=50, unique=True)
    name = models.CharField("档案名称", max_length=200)
    cabinet = models.ForeignKey(
        Cabinet, on_delete=models.PROTECT, related_name="archives", verbose_name="所属档案柜"
    )
    layer = models.PositiveSmallIntegerField("层号")
    cell = models.PositiveSmallIntegerField("格号")
    file = models.FileField(
        "电子档案",
        upload_to=archive_file_upload_to,
        null=True,
        blank=True,
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="archives",
        verbose_name="类别",
    )
    tags = models.CharField("标签", max_length=200, blank=True, help_text="多个标签用逗号分隔")
    description = models.TextField("档案描述", blank=True)
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_archives",
        verbose_name="创建人",
    )

    class Meta:
        verbose_name = "档案"
        verbose_name_plural = "档案"
        ordering = ["cabinet__code", "layer", "cell"]
        unique_together = [("cabinet", "layer", "cell")]

    def __str__(self):
        return f"{self.code} - {self.name}"

    @property
    def location_path(self) -> str:
        """完整物理路径：柜-层-格。"""
        return f"{self.cabinet.code}-{self.layer}层-{self.cell}格"

    @property
    def is_borrowed(self) -> bool:
        """是否处于借出状态。"""
        return self.borrow_records.filter(return_time__isnull=True).exists()

    @property
    def tags_list(self) -> list:
        """标签列表。"""
        return [t.strip() for t in self.tags.split(",") if t.strip()]


class BorrowRecord(models.Model):
    """档案借阅记录。"""

    class Status(models.TextChoices):
        BORROWED = "borrowed", "借出中"
        RETURNED = "returned", "已归还"
        OVERDUE = "overdue", "已逾期"

    archive = models.ForeignKey(
        Archive, on_delete=models.CASCADE, related_name="borrow_records", verbose_name="档案"
    )
    borrower = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="borrow_records",
        verbose_name="借阅人",
    )
    borrow_time = models.DateTimeField("借阅时间", default=timezone.now)
    due_time = models.DateField("应归还日期")
    return_time = models.DateTimeField("实际归还时间", null=True, blank=True)
    status = models.CharField("状态", max_length=10, choices=Status.choices, default=Status.BORROWED)
    remark = models.CharField("备注", max_length=200, blank=True)

    class Meta:
        verbose_name = "借阅记录"
        verbose_name_plural = "借阅记录"
        ordering = ["-borrow_time"]

    def __str__(self):
        return f"{self.archive.code} - {self.borrower.username}"

    @property
    def is_overdue(self) -> bool:
        """是否逾期（未归还且超过应还日期）。"""
        if self.return_time:
            return False
        return timezone.now().date() > self.due_time

    def refresh_status(self):
        """根据归还情况和日期刷新状态字段。"""
        if self.return_time:
            self.status = self.Status.RETURNED
        elif self.is_overdue:
            self.status = self.Status.OVERDUE
        else:
            self.status = self.Status.BORROWED
