"""档案管理模块数据模型。

模型说明：
- Cabinet              档案柜（编号、位置、层数、每层格数）
- Category             档案类别（树形多级结构）
- Archive              档案（编号、名称、柜/层/格定位、电子文件、类别、标签）
- BorrowRecord         借阅记录（借阅人、借出/应还/实还时间、状态）
- License              证照（编号、持证单位、发证机关、有效期、年审周期）
- AnnualReview         年审记录（证照外键、年审日期、结果、新有效期）
- LicenseBorrowRecord  证照借阅登记

操作日志复用 records.models.OperationLog，避免重复建设。
"""

import calendar
import os
from datetime import date, timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


def _add_months(d, months):
    """日期加月数，自动处理月末溢出（如 1/31 + 1 月 = 2/28）。"""
    if not d or not months:
        return d
    month_idx = d.month - 1 + months
    year = d.year + month_idx // 12
    month = month_idx % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


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
    """档案：物理定位 + 电子文件 + 分类标签 + 业务台账字段。"""

    code = models.CharField("档案编号", max_length=50, unique=True)
    name = models.CharField("档案名称", max_length=200)
    cabinet = models.ForeignKey(
        Cabinet, on_delete=models.PROTECT, related_name="archives", verbose_name="所属档案柜",
        null=True, blank=True,
    )
    layer = models.PositiveSmallIntegerField("层号", null=True, blank=True)
    cell = models.PositiveSmallIntegerField("格号", null=True, blank=True)
    location_code = models.CharField("位置编码", max_length=50, blank=True, help_text="文本位置编码，如 09-3-7-1")
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
    # ---- 业务台账字段（对应 Excel 台账列） ----
    counterpart = models.CharField("公司名称/相对方", max_length=200, blank=True, help_text="合同相对方/供应商/发文单位")
    contract_no = models.CharField("合同编号", max_length=100, blank=True)
    amount = models.CharField("费用/金额", max_length=50, blank=True, help_text="文本格式，如 135956.8 或 300万")
    sign_date = models.DateField("签订日期", null=True, blank=True)
    expire_date = models.DateField("截止/到期日期", null=True, blank=True)
    department = models.CharField("部门", max_length=50, blank=True)
    security_level = models.CharField("保密等级", max_length=20, blank=True, help_text="机密/一般/...")
    archive_label = models.CharField("档案标签", max_length=100, blank=True, help_text="如 专利合同(一)、保险合同(一)")
    handler = models.CharField("移交人/经办人", max_length=50, blank=True)
    copies = models.PositiveSmallIntegerField("份数", null=True, blank=True)
    attachment_desc = models.CharField("附件描述", max_length=200, blank=True)
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
        ordering = ["-created_at"]
        # 柜层格唯一约束仅在三者均非空时生效（通过 DB partial index）

    def __str__(self):
        return f"{self.code} - {self.name}"

    @property
    def location_path(self) -> str:
        """完整物理路径：优先文本位置编码，其次柜-层-格。"""
        if self.location_code:
            return self.location_code
        if self.cabinet_id:
            return f"{self.cabinet.code}-{self.layer}层-{self.cell}格"
        return "—"

    @property
    def is_borrowed(self) -> bool:
        """是否处于借出状态。"""
        return self.borrow_records.filter(return_time__isnull=True).exists()

    @property
    def is_expiring(self) -> bool:
        """是否即将到期（30天内）。"""
        if not self.expire_date:
            return False
        from datetime import date, timedelta
        return self.expire_date <= date.today() + timedelta(days=30)

    @property
    def tags_list(self) -> list:
        """标签列表。"""
        return [t.strip() for t in self.tags.split(",") if t.strip()]

    @property
    def file_ext(self) -> str:
        """电子文件扩展名（大写），无文件时返回空串。"""
        if not self.file:
            return ""
        return os.path.splitext(self.file.name)[1].lstrip(".").upper()

    @property
    def file_basename(self) -> str:
        """电子文件纯文件名（不含 media 路径）。"""
        if not self.file:
            return ""
        return os.path.basename(self.file.name)

    @property
    def is_image(self) -> bool:
        """电子文件是否为图片（可内嵌预览）。"""
        return self.file_ext in {"PNG", "JPG", "JPEG", "GIF", "BMP", "WEBP"}

    @property
    def is_pdf(self) -> bool:
        """电子文件是否为 PDF（可内嵌预览）。"""
        return self.file_ext == "PDF"


class BorrowRecord(models.Model):
    """档案借阅记录。"""

    class Status(models.TextChoices):
        BORROWED = "borrowed", "借出中"
        RETURNED = "returned", "已归还"
        OVERDUE = "overdue", "已逾期"

    archive = models.ForeignKey(
        Archive, on_delete=models.CASCADE, related_name="borrow_records", verbose_name="档案"
    )
    borrower = models.CharField("借阅人", max_length=50, help_text="借阅人姓名（可为系统外人员）")
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
        return f"{self.archive.code} - {self.borrower}"

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


# ==================== 证照管理子模块 ====================


def license_scan_upload_to(instance, filename):
    """证照扫描件上传路径：media/licenses/<证照编号>/<文件名>。"""
    code = instance.code or "unknown"
    return os.path.join("licenses", code, os.path.basename(filename))


class License(TimeStampedModel):
    """证照：企业持有的各类证照（营业执照、资质证书等），支持年审与借阅。"""

    STATUS_CHOICES = [
        ("normal", "正常"),
        ("expiring", "即将到期"),
        ("expired", "已过期"),
    ]

    name = models.CharField("证照名称", max_length=200)
    code = models.CharField("证照编号", max_length=100, unique=True)
    holder = models.CharField("持证单位", max_length=200, blank=True)
    issuer = models.CharField("发证机关", max_length=200, blank=True)
    issue_date = models.DateField("发证日期", null=True, blank=True)
    expire_date = models.DateField("有效期至", null=True, blank=True)
    review_cycle = models.PositiveSmallIntegerField(
        "年审周期（月）", default=0, help_text="0=不需年审，12=每年一次，24=每两年一次"
    )
    next_review_date = models.DateField("下次年审日", null=True, blank=True)
    scan_file = models.FileField(
        "证照扫描件", upload_to=license_scan_upload_to, null=True, blank=True
    )
    remark = models.TextField("备注", blank=True)
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_licenses",
        verbose_name="创建人",
    )

    class Meta:
        verbose_name = "证照"
        verbose_name_plural = "证照"
        ordering = ["-id"]

    def __str__(self):
        return f"{self.code} - {self.name}"

    def save(self, *args, **kwargs):
        """保存时自动计算下次年审日（若未手动设置且有发证日期+年审周期）。"""
        if not self.next_review_date and self.review_cycle and self.issue_date:
            self.next_review_date = _add_months(self.issue_date, self.review_cycle)
        super().save(*args, **kwargs)

    @property
    def status(self) -> str:
        """状态：normal / expiring / expired（基于有效期和下次年审日）。"""
        today = date.today()
        threshold = today + timedelta(days=30)
        # 已过期：有效期或下次年审日已过
        if self.expire_date and self.expire_date < today:
            return "expired"
        if self.next_review_date and self.next_review_date < today:
            return "expired"
        # 即将到期：30 天内到期或年审
        if self.expire_date and self.expire_date <= threshold:
            return "expiring"
        if self.next_review_date and self.next_review_date <= threshold:
            return "expiring"
        return "normal"

    @property
    def status_display(self) -> str:
        return dict(self.STATUS_CHOICES).get(self.status, "正常")

    @property
    def is_expiring(self) -> bool:
        return self.status == "expiring"

    @property
    def is_expired(self) -> bool:
        return self.status == "expired"

    @property
    def is_borrowed(self) -> bool:
        """是否处于借出状态。"""
        return self.borrow_records.filter(return_time__isnull=True).exists()

    @property
    def scan_ext(self) -> str:
        """扫描件扩展名（大写），无文件时返回空串。"""
        if not self.scan_file:
            return ""
        return os.path.splitext(self.scan_file.name)[1].lstrip(".").upper()

    @property
    def scan_basename(self) -> str:
        """扫描件纯文件名。"""
        if not self.scan_file:
            return ""
        return os.path.basename(self.scan_file.name)

    @property
    def is_image(self) -> bool:
        return self.scan_ext in {"PNG", "JPG", "JPEG", "GIF", "BMP", "WEBP"}

    @property
    def is_pdf(self) -> bool:
        return self.scan_ext == "PDF"


class AnnualReview(TimeStampedModel):
    """年审记录：每次年审操作的明细。"""

    RESULT_CHOICES = [
        ("passed", "通过"),
        ("failed", "未通过"),
        ("pending", "待处理"),
    ]

    license = models.ForeignKey(
        License,
        on_delete=models.CASCADE,
        related_name="reviews",
        verbose_name="证照",
    )
    review_date = models.DateField("年审日期", default=date.today)
    result = models.CharField("年审结果", max_length=10, choices=RESULT_CHOICES, default="passed")
    reviewer = models.CharField("经办人", max_length=50)
    new_expire_date = models.DateField("新有效期至", null=True, blank=True, help_text="年审通过后的新有效期（可选）")
    remark = models.CharField("备注", max_length=500, blank=True)

    class Meta:
        verbose_name = "年审记录"
        verbose_name_plural = "年审记录"
        ordering = ["-review_date", "-id"]

    def __str__(self):
        return f"{self.license.code} - {self.review_date} ({self.get_result_display()})"


class LicenseBorrowRecord(models.Model):
    """证照借阅登记（支持系统外人员借阅）。"""

    class Status(models.TextChoices):
        BORROWED = "borrowed", "借出中"
        RETURNED = "returned", "已归还"
        OVERDUE = "overdue", "已逾期"

    license = models.ForeignKey(
        License,
        on_delete=models.CASCADE,
        related_name="borrow_records",
        verbose_name="证照",
    )
    borrower = models.CharField("借阅人", max_length=50, help_text="借阅人姓名（可为系统外人员）")
    borrow_time = models.DateTimeField("借阅时间", default=timezone.now)
    due_time = models.DateField("应归还日期")
    return_time = models.DateTimeField("实际归还时间", null=True, blank=True)
    status = models.CharField("状态", max_length=10, choices=Status.choices, default=Status.BORROWED)
    remark = models.CharField("备注", max_length=200, blank=True)

    class Meta:
        verbose_name = "证照借阅记录"
        verbose_name_plural = "证照借阅记录"
        ordering = ["-borrow_time"]

    def __str__(self):
        return f"{self.license.code} - {self.borrower}"

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
