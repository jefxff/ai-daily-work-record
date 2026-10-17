"""数据备份与恢复操作记录模型。

独立于 records.OperationLog：新增「操作结果」字段并覆盖失败操作，
确保备份/恢复全流程（成功与失败）轨迹完整留存。
"""

import uuid

from django.db import models
from django.utils import timezone


class BackupOperationLog(models.Model):
    """备份/恢复操作记录（含失败记录）。

    恢复操作会将数据库整体回退，本表数据可能随备份包回滚丢失，
    services 层同时将记录追加写入 backups/operation_logs.jsonl，
    恢复完成后由该文件自动回填，保证轨迹无遗漏。
    """

    OP_BACKUP = "backup"
    OP_RESTORE = "restore"
    OP_CHOICES = [(OP_BACKUP, "备份导出"), (OP_RESTORE, "数据恢复")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    op_type = models.CharField("操作类型", max_length=20, choices=OP_CHOICES)
    operator = models.CharField("操作人", max_length=100, default="-")
    file_name = models.CharField("操作文件标识", max_length=255, default="", blank=True)
    file_path = models.CharField("文件完整路径", max_length=600, default="", blank=True)
    success = models.BooleanField("操作结果", default=True)
    detail = models.TextField("操作详情", default="", blank=True)
    created_at = models.DateTimeField("操作时间", default=timezone.now)

    class Meta:
        verbose_name = "备份操作记录"
        verbose_name_plural = "备份操作记录"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["op_type", "-created_at"], name="idx_bkop_type_time"),
        ]

    def __str__(self):
        return f"{self.get_op_type_display()} {self.file_name or '(未生成文件)'} {'成功' if self.success else '失败'}"
