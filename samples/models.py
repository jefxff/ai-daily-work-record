"""试样跟进管理核心数据模型（集成自独立 SEM 系统）。

模型字段与《试样跟进程序精简方案》6.2 节一一对应：
- Partner       供应商档案
- Sample        样品主档
- TestRecord    试样记录表（多轮试产，一对多）
- BatchRecord   小批量记录表（多批验证，一对多）
- AuditRecord   新供应商审厂记录（按供应商维度管理）
- ProofAttachment 业务流程证明材料（6 个关键节点凭证）
- OperationLog  全流程操作留痕

与原独立系统的差异：本站无登录认证，原 FK(User) 的操作人/登记人/
上传人字段统一改为开放填写的 CharField，与全站其他模块保持一致。
"""

from django.db import models
from django.utils import timezone


class TimeStampedModel(models.Model):
    """统一创建/更新时间基类。"""

    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        abstract = True


class Partner(TimeStampedModel):
    """供应商档案（独立维护，被样品引用）。"""

    class CooperationStatus(models.TextChoices):
        ACTIVE = "active", "合作中"
        INACTIVE = "inactive", "已停用"

    partner_code = models.CharField("档案编号", max_length=20, unique=True, blank=True)
    name = models.CharField("名称", max_length=200, unique=True)
    contact_person = models.CharField("联系人", max_length=50, blank=True)
    contact_info = models.CharField("联系方式", max_length=100, blank=True)
    status = models.CharField(
        "合作状态",
        max_length=10,
        choices=CooperationStatus.choices,
        default=CooperationStatus.ACTIVE,
    )

    class Meta:
        verbose_name = "供应商档案"
        verbose_name_plural = verbose_name
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name}（{self.partner_code}）"

    @property
    def status_badge(self):
        return "success" if self.status == self.CooperationStatus.ACTIVE else "default"

    @property
    def is_referenced(self):
        """是否已被样品主档引用（引用后禁止删除）。"""
        return Sample.objects.filter(supplier=self).exists()


class Sample(TimeStampedModel):
    """样品主档（核心主数据）。"""

    class Status(models.TextChoices):
        PENDING = "pending", "待送样"
        TESTING = "testing", "试样中"
        SMALL_BATCH = "small_batch", "小批量中"
        IMPORTED = "imported", "已正式导入"
        TERMINATED = "terminated", "已终止"

    class SupplierType(models.TextChoices):
        NEW = "new", "新增供应商"
        EXISTING = "existing", "已合作供应商"

    # 状态对应全站 status-badge 配色
    STATUS_BADGE = {
        Status.PENDING: "default",
        Status.TESTING: "info",
        Status.SMALL_BATCH: "warning",
        Status.IMPORTED: "success",
        Status.TERMINATED: "danger",
    }

    sample_no = models.CharField("样品编号", max_length=30, unique=True, blank=True, db_index=True)
    material_name = models.CharField("物料名称", max_length=200)
    model = models.CharField("型号", max_length=100)
    sample_date = models.DateField("送样时间", db_index=True)
    status = models.CharField(
        "当前状态", max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    registrar = models.CharField("登记人", max_length=50, blank=True)
    registered_at = models.DateTimeField("登记时间", default=timezone.now)

    # 可选字段
    spec = models.CharField("物料规格", max_length=200, blank=True)
    incoming_qty = models.CharField("来料数量", max_length=50, blank=True)
    supplier = models.ForeignKey(
        Partner,
        verbose_name="供应商",
        on_delete=models.PROTECT,
        related_name="supplier_samples",
        null=True,
        blank=True,
    )
    supplier_type = models.CharField(
        "供应商类型", max_length=10, choices=SupplierType.choices,
        default=SupplierType.EXISTING, db_index=True,
    )

    # 终止信息
    terminate_reason = models.TextField("终止原因", blank=True)
    terminated_by = models.CharField("终止操作人", max_length=50, blank=True)
    terminated_at = models.DateTimeField("终止时间", null=True, blank=True)

    # 正式导入确认信息
    imported_by = models.CharField("导入确认人", max_length=50, blank=True)
    imported_at = models.DateTimeField("正式导入时间", null=True, blank=True)

    # 软删除（防误删）
    is_deleted = models.BooleanField("已删除", default=False, db_index=True)
    deleted_by = models.CharField("删除人", max_length=50, blank=True)
    deleted_at = models.DateTimeField("删除时间", null=True, blank=True)

    class Meta:
        verbose_name = "样品主档"
        verbose_name_plural = verbose_name
        ordering = ["-sample_date", "-id"]

    def __str__(self):
        return f"{self.sample_no} {self.material_name}/{self.model}"

    @property
    def status_badge_class(self):
        return self.STATUS_BADGE.get(self.status, "default")

    @property
    def is_terminal(self):
        return self.status in (self.Status.IMPORTED, self.Status.TERMINATED)

    @property
    def latest_test_round(self):
        record = self.test_records.first()
        return record.round_no if record else 0

    @property
    def latest_batch_round(self):
        record = self.batch_records.first()
        return record.round_no if record else 0


class TestRecord(TimeStampedModel):
    """试样记录表（支持无限轮试产）。"""

    class Conclusion(models.TextChoices):
        PASS = "pass", "通过"
        FAIL = "fail", "不通过"
        IMPROVE = "improve", "待改进"

    CONCLUSION_BADGE = {
        Conclusion.PASS: "success",
        Conclusion.FAIL: "danger",
        Conclusion.IMPROVE: "warning",
    }

    test_no = models.CharField("试样编号", max_length=30, unique=True, blank=True, db_index=True)
    sample = models.ForeignKey(
        Sample, verbose_name="关联样品", on_delete=models.CASCADE, related_name="test_records"
    )
    round_no = models.PositiveIntegerField("试产轮次")
    test_date = models.DateField("试产日期", db_index=True)
    test_result = models.TextField("测试结果")
    review_conclusion = models.CharField(
        "评审结论", max_length=10, choices=Conclusion.choices
    )
    report_code = models.CharField("验证报告编码", max_length=100)
    notified = models.BooleanField("是否通知责任人", default=False)
    operator = models.CharField("操作人", max_length=50, blank=True)
    operated_at = models.DateTimeField("操作时间", default=timezone.now)

    # 可选字段
    package_type = models.CharField("封装形式", max_length=100, blank=True)
    incoming_date = models.DateField("来料日期", null=True, blank=True)
    remark = models.TextField("备注", blank=True)

    class Meta:
        verbose_name = "试样记录"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=["sample", "round_no"], name="uniq_test_round_per_sample"
            )
        ]
        ordering = ["-round_no"]

    def __str__(self):
        return f"{self.test_no} 第{self.round_no}轮"

    @property
    def conclusion_badge(self):
        return self.CONCLUSION_BADGE.get(self.review_conclusion, "default")


class BatchRecord(TimeStampedModel):
    """小批量记录表（支持无限批验证）。"""

    class Result(models.TextChoices):
        PASS = "pass", "通过"
        FAIL = "fail", "不通过"
        ONGOING = "ongoing", "进行中"

    RESULT_BADGE = {
        Result.PASS: "success",
        Result.FAIL: "danger",
        Result.ONGOING: "warning",
    }

    batch_no = models.CharField("小批量编号", max_length=30, unique=True, blank=True, db_index=True)
    sample = models.ForeignKey(
        Sample, verbose_name="关联样品", on_delete=models.CASCADE, related_name="batch_records"
    )
    round_no = models.PositiveIntegerField("批次轮次")
    ordered = models.BooleanField("是否下单", default=False)
    batch_result = models.CharField(
        "小批量结果", max_length=10, choices=Result.choices
    )
    kingdee_entered = models.BooleanField("是否录入金蝶", default=False)
    operator = models.CharField("操作人", max_length=50, blank=True)
    operated_at = models.DateTimeField("操作时间", default=timezone.now)

    # 可选字段
    unit_price = models.DecimalField(
        "试样单价", max_digits=12, decimal_places=2, null=True, blank=True
    )
    alternative_model = models.CharField("可替代型号", max_length=100, blank=True)
    remark = models.TextField("批次备注", blank=True)

    class Meta:
        verbose_name = "小批量记录"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=["sample", "round_no"], name="uniq_batch_round_per_sample"
            )
        ]
        ordering = ["-round_no"]

    def __str__(self):
        return f"{self.batch_no} 第{self.round_no}批"

    @property
    def result_badge(self):
        return self.RESULT_BADGE.get(self.batch_result, "default")


def audit_file_path(instance, filename):
    """审厂记录文件按 供应商编号/原文件名 分目录存储。"""
    partner_code = instance.partner.partner_code if instance.partner_id else "unbound"
    return f"audits/{partner_code}/{filename}"


class AuditRecord(TimeStampedModel):
    """新供应商审厂记录（支持多轮审厂，按供应商维度管理）。

    审厂针对供应商（工厂）而非单款样品；同一供应商送多款样品时
    审厂记录只需录一次，所有样品共享。
    """

    class Result(models.TextChoices):
        PASS = "pass", "通过"
        FAIL = "fail", "不通过"
        CONDITIONAL = "conditional", "有条件通过"

    RESULT_BADGE = {
        Result.PASS: "success",
        Result.FAIL: "danger",
        Result.CONDITIONAL: "warning",
    }

    audit_no = models.CharField("审厂编号", max_length=30, unique=True, blank=True, db_index=True)
    partner = models.ForeignKey(
        Partner, verbose_name="供应商", on_delete=models.CASCADE, related_name="audit_records",
    )
    round_no = models.PositiveIntegerField("审厂轮次")
    audit_date = models.DateField("审厂日期", db_index=True)
    audit_result = models.CharField("审厂结果", max_length=12, choices=Result.choices)
    audit_remark = models.TextField("审厂备注", blank=True)
    auditor = models.CharField("操作人", max_length=50, blank=True)
    operated_at = models.DateTimeField("操作时间", default=timezone.now)
    file = models.FileField("审核记录文件", upload_to=audit_file_path, max_length=500)

    class Meta:
        verbose_name = "审厂记录"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=["partner", "round_no"], name="uniq_audit_round_per_partner"
            )
        ]
        ordering = ["-round_no"]

    def __str__(self):
        return f"{self.audit_no} 第{self.round_no}轮"

    @property
    def result_badge(self):
        return self.RESULT_BADGE.get(self.audit_result, "default")

    @property
    def is_image(self):
        name = (self.file.name or "").lower() if self.file else ""
        return name.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))


def proof_upload_path(instance, filename):
    """证明材料按 样品编号/阶段/原文件名 分目录存储。"""
    sample_no = instance.sample.sample_no if instance.sample_id else "unbound"
    return f"proofs/{sample_no}/{instance.stage}/{filename}"


class ProofAttachment(TimeStampedModel):
    """业务流程证明材料附件（全流程 6 个关键节点凭证）。"""

    class Stage(models.TextChoices):
        TEST_REPORT = "test_report", "试样报告"
        PRICE_LIST_UPDATE = "price_list_update", "价目表更新凭证"
        SMALL_BATCH_NOTIFICATION = "small_batch_notification", "小批量阶段通知邮件"
        SMALL_BATCH_ORDER = "small_batch_order", "小批量采购订单"
        SMALL_BATCH_RESULT = "small_batch_result", "小批量结果凭证"
        MASS_PRODUCTION_ORDER = "mass_production_order", "量产采购订单"

    # 阶段对应全站 status-badge 配色（卡片左侧色条与徽标）
    STAGE_BADGE = {
        Stage.TEST_REPORT: "info",
        Stage.PRICE_LIST_UPDATE: "info",
        Stage.SMALL_BATCH_NOTIFICATION: "info",
        Stage.SMALL_BATCH_ORDER: "warning",
        Stage.SMALL_BATCH_RESULT: "warning",
        Stage.MASS_PRODUCTION_ORDER: "success",
    }

    # 阶段业务说明（详情页卡片副标题）
    STAGE_DESC = {
        Stage.TEST_REPORT: "上传试样报告，证明试样截断流程已正式结束，确认试样是否通过",
        Stage.PRICE_LIST_UPDATE: "试样通过后在金蝶系统更新价目表，截取相关界面作为凭证",
        Stage.SMALL_BATCH_NOTIFICATION: "邮件通知指定人员试样已通过并可进入小批量阶段，截取发送成功界面",
        Stage.SMALL_BATCH_ORDER: "在金蝶采购订单模块查找小批量采购记录，截取订单信息界面作为凭证",
        Stage.SMALL_BATCH_RESULT: "小批量生产完成后上传邮件截图或小批量生产报告作为凭证",
        Stage.MASS_PRODUCTION_ORDER: "小批量结果正常时，截取金蝶正常采购订单界面作为凭证",
    }

    # 阶段对应图标（emoji，无需图标字体）
    STAGE_ICON = {
        Stage.TEST_REPORT: "🧪",
        Stage.PRICE_LIST_UPDATE: "💱",
        Stage.SMALL_BATCH_NOTIFICATION: "✉️",
        Stage.SMALL_BATCH_ORDER: "🛒",
        Stage.SMALL_BATCH_RESULT: "📊",
        Stage.MASS_PRODUCTION_ORDER: "🚚",
    }

    sample = models.ForeignKey(
        Sample,
        verbose_name="关联样品",
        on_delete=models.CASCADE,
        related_name="proof_attachments",
    )
    stage = models.CharField("业务阶段", max_length=30, choices=Stage.choices, db_index=True)
    file = models.FileField("证明文件", upload_to=proof_upload_path, max_length=500)
    original_name = models.CharField("原始文件名", max_length=255)
    uploaded_by = models.CharField("上传人", max_length=50, blank=True)
    remark = models.TextField("备注", blank=True)

    class Meta:
        verbose_name = "证明材料"
        verbose_name_plural = verbose_name
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["sample", "stage"]),
        ]

    def __str__(self):
        return f"{self.get_stage_display()} · {self.original_name}"

    @property
    def stage_badge(self):
        return self.STAGE_BADGE.get(self.stage, "default")

    @property
    def stage_desc(self):
        return self.STAGE_DESC.get(self.stage, "")

    @property
    def stage_icon(self):
        return self.STAGE_ICON.get(self.stage, "📎")

    @property
    def is_image(self):
        name = (self.file.name or "").lower()
        return name.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))
