"""通用附件模型（业务记录的多附件上传）。

由来：档案（``ArchiveFile``，一档多附件）与样品（``ProofAttachment``，按业务
阶段分组）各自实现了一套「主记录 → 附件」的一对多结构，客户客诉/诉讼/保险/
客户台账等模块又只有零散的「文本字段描述证据」而没有文件留存。本模型把这两处
已验证的模式泛化：任意业务模型通过 ``content_type + object_id`` 挂接任意多个
附件，上传/删除/缩略图/鉴权/留痕逻辑全站只写一份。

设计约束：

1. **模块登记制**：``MODEL_MODULE_MAP`` 把 (app_label, model) 映射到 RBAC
   模块 key（``accounts/registry.py``）。未登记的模型一律 404 —— 与媒体文件
   的模块级鉴权（``work_record/urls.py::media_module_for``）同源，新模块想
   接附件必须先登记，避免「上传了但打不开/谁都能看」。
2. **路径结构**：``attachments/<模块key>/<模型名>/<对象id>/<随机名><扩展名>``，
   一级目录固定为 attachments，二级目录即模块 key，媒体鉴权按此判定归属。
3. **不级联删文件**：业务记录删除时由调用方（``services.delete_for_object``）
   显式清理，避免 Django 的 bulk/级联删除留下磁盘孤儿文件。
"""

import os
import uuid

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

# ---------------------------------------------------------------- 常量与映射

# 业务模型 → RBAC 模块 key。新增接入模块时在此登记（views / media 鉴权同源）。
MODEL_MODULE_MAP = {
    ("records", "customercomplaint"): "customer_complaint",
    ("records", "complaint"): "complaint",
    ("records", "insurance"): "insurance",
    ("records", "customerledger"): "customer_ledger",
}

# 各模块的业务分类（上传时可选，列表里显示为徽标）；空列表 = 不显示分类选择。
ATTACHMENT_CATEGORIES = {
    "customer_complaint": [
        ("accept", "受理证据"),
        ("process", "处理过程"),
        ("closure", "闭环资料"),
    ],
    "complaint": [
        ("filing", "起诉材料"),
        ("evidence", "证据材料"),
        ("hearing", "庭审文书"),
        ("execution", "执行材料"),
    ],
    "insurance": [
        ("policy", "电子保单"),
        ("endorsement", "批单"),
        ("receipt", "缴费凭证"),
        ("claim", "理赔材料"),
    ],
    "customer_ledger": [],
}

# 允许的扩展名与大小上限（与档案附件同一白名单；上限取 20MB，见 validators）
ATTACHMENT_ALLOWED_EXTS = {
    "PNG", "JPG", "JPEG", "GIF", "BMP", "WEBP",
    "PDF", "DOC", "DOCX", "XLS", "XLSX", "PPT", "PPTX",
    "TXT", "CSV", "ZIP", "RAR", "7Z",
}
ATTACHMENT_MAX_SIZE = 20 * 1024 * 1024

IMAGE_EXTS = {"PNG", "JPG", "JPEG", "GIF", "BMP", "WEBP"}


def attachment_upload_to(instance, filename):
    """落盘路径：attachments/<模块key>/<模型名>/<对象id>/<随机名>.<原扩展名>。

    随机名避免中文/特殊字符文件名与同名冲突；保留原扩展名供浏览器识别。
    """
    ext = os.path.splitext(filename or "")[1].lower()
    ct = instance.content_type
    module = MODEL_MODULE_MAP.get((ct.app_label, ct.model), "misc") if ct else "misc"
    model_name = ct.model if ct else "unknown"
    return f"attachments/{module}/{model_name}/{instance.object_id}/{uuid.uuid4().hex[:12]}{ext}"


class TimeStampedModel(models.Model):
    """统一创建/更新时间基类（与 archive/samples 同构）。"""

    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        abstract = True


class Attachment(TimeStampedModel):
    """通用附件：一条业务记录可挂多个文件。

    - content_type + object_id：泛化外键，指向任意已登记的业务模型；
    - category：业务分类（取值见 ATTACHMENT_CATEGORIES，空串 = 未分类）；
    - size：上传时的字节数，列表展示用（不重复 stat 磁盘）。
    """

    content_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, verbose_name="业务对象类型"
    )
    object_id = models.PositiveIntegerField("业务对象ID", db_index=True)
    content_object = GenericForeignKey("content_type", "object_id")

    file = models.FileField("附件文件", upload_to=attachment_upload_to, max_length=500)
    original_name = models.CharField("原始文件名", max_length=255)
    size = models.PositiveIntegerField("文件大小(字节)", default=0)
    category = models.CharField(
        "业务分类", max_length=30, blank=True, default="", db_index=True
    )
    remark = models.CharField("备注", max_length=255, blank=True, default="")
    uploaded_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="uploaded_attachments",
        verbose_name="上传人",
    )

    class Meta:
        verbose_name = "附件"
        verbose_name_plural = "附件"
        ordering = ["-id"]
        indexes = [
            # 列表页按业务对象批量取附件（services.attachments_for / inject_counts）
            models.Index(fields=["content_type", "object_id"], name="idx_att_obj"),
        ]

    def __str__(self):
        return f"{self.content_type.model}#{self.object_id} - {self.original_name}"

    # ---------- 展示便捷属性（与 archive.ArchiveFile 同构） ----------

    @property
    def module_key(self):
        """所属 RBAC 模块 key（未登记返回空串）。"""
        return MODEL_MODULE_MAP.get(
            (self.content_type.app_label, self.content_type.model), ""
        )

    @property
    def category_label(self):
        """业务分类的中文名（未分类返回空串）。"""
        for value, label in ATTACHMENT_CATEGORIES.get(self.module_key, []):
            if value == self.category:
                return label
        return ""

    @property
    def ext(self):
        """扩展名（大写、不带点），无文件返回空串。"""
        if not self.file:
            return ""
        return os.path.splitext(self.file.name)[1].lstrip(".").upper()

    @property
    def is_image(self):
        return self.ext in IMAGE_EXTS

    @property
    def is_pdf(self):
        return self.ext == "PDF"
