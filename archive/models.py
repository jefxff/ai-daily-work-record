"""档案管理模块数据模型。

模型说明：
- Cabinet              档案柜（编号、位置、层数、每层格数）
- Category             档案类别（树形多级结构）
- Ledger               台账（对应一份 Excel 台账文件，档案的逻辑分组）
- Archive              档案（编号、名称、柜/层/格定位、所属台账、类别、标签）
- ArchiveFile          档案电子附件（一条档案可挂多个文件）
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
from django.core.exceptions import ValidationError
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
    """档案柜：定义物理存放位置的层级结构（柜 -> 层 -> 格），每层格数可独立设置。"""

    code = models.CharField("档案柜编号", max_length=50, unique=True)
    location = models.CharField("所在位置", max_length=200, blank=True)
    layers = models.PositiveSmallIntegerField("层数", default=4)

    class Meta:
        verbose_name = "档案柜"
        verbose_name_plural = "档案柜"
        ordering = ["code"]

    def __str__(self):
        return f"{self.code}（{self.location}）"

    def cells_of_layer(self, layer_no: int):
        """指定层的格数；无层记录时返回 None（调用方自行决定是否校验）。"""
        row = self.layer_configs.filter(layer_no=layer_no).first()
        return row.cells if row else None

    def layer_cells_summary(self) -> str:
        """各层格数摘要，如 "10,10,8,8"；无层记录时返回 "—"。"""
        rows = list(self.layer_configs.order_by("layer_no"))
        return ",".join(str(r.cells) for r in rows) if rows else "—"

    @property
    def total_cells(self) -> int:
        """档案柜总格子数（各层格数之和）。"""
        return sum(r.cells for r in self.layer_configs.all())

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
        for lc in self.layer_configs.order_by("layer_no"):
            matrix.append([archives.get((lc.layer_no, cell)) for cell in range(1, lc.cells + 1)])
        return matrix

    def sync_layers(self, cells=None):
        """把层记录同步为 layers 层，逐层格数取 cells 列表（1..layers）。

        cells 为 None 时：保留现有层记录格数，缺失的层用默认 10 格补齐，
        超出 layers 的多余层删除。缩层/缩格时若已有档案占用则抛 ValueError。
        """
        if cells is None:
            existing = {r.layer_no: r.cells for r in self.layer_configs.all()}
            cells = [existing.get(i, 10) for i in range(1, self.layers + 1)]
        if len(cells) != self.layers:
            raise ValueError(f"格数配置数量（{len(cells)}）与层数（{self.layers}）不一致")
        cleaned = []
        for no, c in enumerate(cells, start=1):
            try:
                c = int(c)
            except (TypeError, ValueError):
                raise ValueError(f"第 {no} 层格数需为数字，收到「{c}」")
            if c < 1:
                raise ValueError(f"第 {no} 层格数至少为 1")
            cleaned.append(c)
        # 缩层校验：被删除的层上不能有档案
        if self.pk and self.layers < self.layer_configs.count():
            occupied = self.archives.filter(layer__gt=self.layers).count()
            if occupied:
                raise ValueError(f"第 {self.layers + 1} 层及以上仍有 {occupied} 份档案，请先移出再减少层数")
        # 缩格校验：每层已有档案的格号不能超过新格数
        if self.pk:
            for no, c in enumerate(cleaned, start=1):
                over = self.archives.filter(layer=no, cell__gt=c).count()
                if over:
                    raise ValueError(f"第 {no} 层有 {over} 份档案的格号超过新格数 {c}，请先调整档案位置")
        rows = {r.layer_no: r for r in self.layer_configs.all()}
        for no, c in enumerate(cleaned, start=1):
            row = rows.pop(no, None)
            if row:
                if row.cells != c:
                    row.cells = c
                    row.save(update_fields=["cells"])
            else:
                CabinetLayer.objects.create(cabinet=self, layer_no=no, cells=c)
        for stale in rows.values():
            stale.delete()


class CabinetLayer(models.Model):
    """档案柜层配置：每层可独立设置格数。"""

    cabinet = models.ForeignKey(Cabinet, verbose_name="档案柜", related_name="layer_configs", on_delete=models.CASCADE)
    layer_no = models.PositiveSmallIntegerField("层号")
    cells = models.PositiveSmallIntegerField("本层格数", default=10)

    class Meta:
        verbose_name = "档案柜层"
        verbose_name_plural = "档案柜层"
        unique_together = [("cabinet", "layer_no")]
        ordering = ["layer_no"]

    def __str__(self):
        return f"{self.cabinet.code}-第{self.layer_no}层（{self.cells}格）"


class Category(models.Model):
    """档案类别（树形多级结构，支持父子关系）。

    防环三道防线（缺一不可，见技术审计报告 #2）：
    1. full_path 有硬深度上限——已有环的数据渲染时不挂死；
    2. clean() / save() 校验父链——shell、admin、导入等一切写入路径都挡；
    3. CategoryForm 在选项层排除自身与全部后代——把错误挡在提交前。
    """

    # 实际最大层级为 3；32 层远超业务需要，仅作为"遇到环就停"的兜底
    MAX_DEPTH = 32

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
        """完整类别路径，如 '客户档案/客诉记录'。

        加深度上限而不是 visited 集合：该属性在模板里高频调用，每次分配 set
        的代价高于两个局部变量；环上任意节点都会在 32 步内被截断。
        """
        names = [self.name]
        node = self.parent
        guard = 0
        while node is not None and guard < self.MAX_DEPTH:
            names.insert(0, node.name)
            node = node.parent
            guard += 1
        if guard >= self.MAX_DEPTH:
            return "⚠ 类别层级异常（疑似循环引用）"
        return "/".join(names)

    def _ancestor_ids(self):
        """向上遍历父链，返回祖先 id 列表；遇到环时提前停止。"""
        ids = []
        node = self.parent
        seen = {self.pk} if self.pk else set()
        while node is not None and len(ids) < self.MAX_DEPTH:
            if node.pk in seen:
                break
            seen.add(node.pk)
            ids.append(node.pk)
            node = node.parent
        return ids

    def clean(self):
        """校验父链不成环。ModelForm.is_valid() 会调用到这里。"""
        super().clean()
        if not self.parent_id:
            return
        node = self.parent
        seen = {self.pk} if self.pk else set()
        depth = 0
        while node is not None and depth < self.MAX_DEPTH:
            if node.pk in seen:
                raise ValidationError({"parent": "父类别形成了循环引用，请另选。"})
            seen.add(node.pk)
            node = node.parent
            depth += 1

    def save(self, *args, **kwargs):
        """写入前做一次父链环检测。

        clean() 只在 full_clean() 时执行，而 Category.objects.create()、
        Excel 导入、admin 批量操作都不会调用它——本次修复的核心 P0 正是
        "两次普通编辑即可成环"，所以校验必须放在 save() 这条唯一收口上。
        """
        if self.parent_id:
            self.clean()
        return super().save(*args, **kwargs)



class Ledger(TimeStampedModel):
    """台账：对应一份 Excel 台账文件，作为档案的逻辑分组单位。"""

    name = models.CharField("台账名称", max_length=200, unique=True, help_text="默认取 Excel 文件名去扩展名")
    description = models.CharField("说明", max_length=300, blank=True)
    sort_order = models.IntegerField("排序", default=0)

    class Meta:
        verbose_name = "台账"
        verbose_name_plural = "台账"
        ordering = ["sort_order", "id"]

    def __str__(self):
        return self.name

    @property
    def archive_count(self) -> int:
        """台账内档案数量。"""
        return self.archives.count()


# 档案附件：类型白名单与大小上限（50MB）
ARCHIVE_FILE_ALLOWED_EXTS = {
    "PNG", "JPG", "JPEG", "GIF", "BMP", "WEBP",
    "PDF", "DOC", "DOCX", "XLS", "XLSX", "PPT", "PPTX",
    "ZIP", "RAR", "7Z", "TXT",
}
ARCHIVE_FILE_MAX_SIZE = 50 * 1024 * 1024


def archive_file_upload_to(instance, filename):
    """档案附件上传路径：media/archives/<档案编号>/<原文件名>，按档案隔离、文件名可读。"""
    code = instance.archive.code if instance.archive_id else "unknown"
    # 仅保留文件名，避免路径注入
    return os.path.join("archives", code, os.path.basename(filename))


class Archive(TimeStampedModel):
    """档案：物理定位 + 分类标签 + 业务台账字段（电子附件见 ArchiveFile）。"""

    code = models.CharField("档案编号", max_length=50, unique=True)
    name = models.CharField("档案名称", max_length=200)
    cabinet = models.ForeignKey(
        Cabinet, on_delete=models.PROTECT, related_name="archives", verbose_name="所属档案柜",
        null=True, blank=True,
    )
    layer = models.PositiveSmallIntegerField("层号", null=True, blank=True)
    cell = models.PositiveSmallIntegerField("格号", null=True, blank=True)
    location_code = models.CharField("位置编码", max_length=50, blank=True, help_text="文本位置编码，如 09-3-7-1")
    ledger = models.ForeignKey(
        Ledger, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="archives", verbose_name="所属台账",
    )
    source_sheet = models.CharField("来源 Sheet", max_length=100, blank=True, help_text="导入时来源 Excel sheet 名")
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
        # 柜/层/格唯一约束：仅当三者均非空时生效（partial index）。
        # 这是 DB 层兜底——表单校验（ArchiveForm.clean）与批量导入都是"先查后写"，
        # 并发/双击下会出现静默的"一格两档"脏数据，必须由数据库最终把关。
        constraints = [
            models.UniqueConstraint(
                fields=["cabinet", "layer", "cell"],
                condition=models.Q(
                    cabinet__isnull=False, layer__isnull=False, cell__isnull=False
                ),
                name="uniq_archive_cabinet_layer_cell",
            )
        ]

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
        """是否处于借出状态。

        若查询集已 annotate(_borrowed=Exists(...))，直接用数据库算好的结果，
        避免逐行 EXISTS（审计 #10 的证照列表就是这条路径）。
        """
        cached = getattr(self, "_borrowed", None)
        if cached is not None:
            return bool(cached)
        return self.borrow_records.filter(return_time__isnull=True).exists()

    @property
    def is_expired(self) -> bool:
        """是否已过期（到期日期早于今天）。"""
        if not self.expire_date:
            return False
        from datetime import date
        return self.expire_date < date.today()

    @property
    def is_expiring(self) -> bool:
        """是否即将到期（30天内到期，不含已过期）。"""
        if not self.expire_date or self.is_expired:
            return False
        from datetime import date, timedelta
        return self.expire_date <= date.today() + timedelta(days=30)

    @property
    def tags_list(self) -> list:
        """标签列表。"""
        return [t.strip() for t in self.tags.split(",") if t.strip()]


class ArchiveFile(TimeStampedModel):
    """档案电子附件：一条档案可挂多个文件，按需单文件加载，不影响列表页。"""

    archive = models.ForeignKey(
        Archive, on_delete=models.CASCADE,
        related_name="files", verbose_name="所属档案",
    )
    file = models.FileField("附件文件", upload_to=archive_file_upload_to)

    class Meta:
        verbose_name = "档案附件"
        verbose_name_plural = "档案附件"
        ordering = ["id"]

    def __str__(self):
        return f"{self.archive.code} - {self.file_basename}"

    @property
    def file_ext(self) -> str:
        """附件扩展名（大写），无文件时返回空串。"""
        if not self.file:
            return ""
        return os.path.splitext(self.file.name)[1].lstrip(".").upper()

    @property
    def file_basename(self) -> str:
        """附件纯文件名（不含 media 路径）。"""
        if not self.file:
            return ""
        return os.path.basename(self.file.name)

    @property
    def is_image(self) -> bool:
        """是否为图片（可生成缩略图内嵌预览）。"""
        return self.file_ext in {"PNG", "JPG", "JPEG", "GIF", "BMP", "WEBP"}

    @property
    def is_pdf(self) -> bool:
        """是否为 PDF。"""
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
        # 列表页与 is_borrowed 判定都按 return_time 过滤（审计 #20）
        indexes = [models.Index(fields=["return_time"], name="idx_borrow_return_time")]
        # 同一档案同一时刻只允许一条未归还记录：借出视图是"先查后写"，
        # 双击/并发会让两条借出请求都读到 is_borrowed=False 而生成重复记录。
        constraints = [
            models.UniqueConstraint(
                fields=["archive"],
                condition=models.Q(return_time__isnull=True),
                name="uniq_open_borrow_per_archive",
            )
        ]

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
        # 状态判定与到期提醒都按 expire_date 排序/过滤（审计 #20）
        indexes = [models.Index(fields=["expire_date"], name="idx_license_expire_date")]

    def __str__(self):
        return f"{self.code} - {self.name}"

    def save(self, *args, **kwargs):
        """保存时自动计算下次年审日（若未手动设置且有发证日期+年审周期）。"""
        if not self.next_review_date and self.review_cycle and self.issue_date:
            self.next_review_date = _add_months(self.issue_date, self.review_cycle)
        super().save(*args, **kwargs)

    @property
    def status(self) -> str:
        """状态：normal / expiring / expired（基于有效期和下次年审日）。

        若查询集已 annotate(_status=...)，直接采用数据库算好的值：列表页因此
        不必逐行做 Python 判断（原来 100 行 = 300 次 EXISTS，见审计 #10）。
        """
        cached = getattr(self, "_status", None)
        if cached:
            return cached
        today = timezone.localdate()
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
        """是否处于借出状态。

        若查询集已 annotate(_borrowed=Exists(...))，直接用数据库算好的结果，
        避免逐行 EXISTS（审计 #10 的证照列表就是这条路径）。
        """
        cached = getattr(self, "_borrowed", None)
        if cached is not None:
            return bool(cached)
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
        # 与档案借阅同构：同一证照同时只允许一条未归还记录（并发/双击防护）
        constraints = [
            models.UniqueConstraint(
                fields=["license"],
                condition=models.Q(return_time__isnull=True),
                name="uniq_open_license_borrow_per_license",
            )
        ]

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
