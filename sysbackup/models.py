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


class BackupTask(models.Model):
    """备份/恢复异步任务状态（落库，替代进程内字典）。

    复核报告 P1-B：任务进度原先只存在模块级 `_TASKS` 字典中，进程重启后
    running 状态凭空消失、失败也没有痕迹。改为落库后：
    - 重启后仍可查询历史任务进度与日志；
    - 未正常结束的任务由「心跳超时」判定为中断并标记失败；
    - 恢复操作会整体回退数据库，因此任务行在恢复完成后由 update_or_create 重建。
    """

    STATUS_RUNNING = "running"
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"

    id = models.CharField("任务ID", max_length=32, primary_key=True)
    kind = models.CharField("类型", max_length=10)  # backup / restore
    status = models.CharField("状态", max_length=10, default=STATUS_RUNNING, db_index=True)
    stage = models.CharField("当前阶段", max_length=60, blank=True, default="")
    percent = models.PositiveSmallIntegerField("进度", default=0)
    message = models.TextField("说明", blank=True, default="")
    error = models.TextField("错误", blank=True, default="")
    log = models.JSONField("日志", default=list, blank=True)
    result = models.JSONField("结果", default=dict, blank=True)
    started_at = models.DateTimeField("开始时间", default=timezone.now, db_index=True)
    finished_at = models.DateTimeField("结束时间", null=True, blank=True)
    heartbeat_at = models.DateTimeField("最后心跳", default=timezone.now, db_index=True)

    class Meta:
        verbose_name = "备份任务"
        verbose_name_plural = "备份任务"
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["status", "heartbeat_at"], name="idx_bktask_status_hb"),
        ]

    def __str__(self):
        return f"{self.kind} {self.id[:8]} {self.status}"

