"""通知模块模型：模板表 / 通知主表 / 用户偏好表 / 发送渠道日志表。

用户隔离与「每日记录」同规范：
- Notification.recipient 为隔离根（FK PROTECT）；
- 所有查询经 services._my_notifications 唯一入口，行级校验失败返回 404；
- 渠道日志随通知级联删除，访问经主表归属校验。
"""
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone

# 通知类别（与 RBAC 无关，用于偏好过滤与列表筛选）
TEMPLATE_CATEGORIES = [
    ("business", "业务提醒"),
    ("task", "任务提醒"),
    ("announcement", "系统公告"),
]

# 发送渠道
CHANNELS = [
    ("inapp", "站内通知"),
    ("email", "邮件"),
]

PRIORITY_CHOICES = [("normal", "普通"), ("high", "高")]


class NotificationTemplate(models.Model):
    """通知模板（全局，管理员维护）。变量占位符使用 {{变量}}，Django Template 引擎渲染。"""

    code = models.CharField("模板编码", max_length=100, unique=True, db_index=True)
    name = models.CharField("模板名称", max_length=100)
    description = models.TextField("用途说明", blank=True, default="")
    title_template = models.CharField("站内标题模板", max_length=200)
    content_template = models.TextField("站内正文模板")
    email_subject_template = models.CharField(
        "邮件主题模板", max_length=200, null=True, blank=True,
        help_text="留空表示该模板不支持邮件渠道",
    )
    email_body_template = models.TextField("邮件正文模板", null=True, blank=True)
    category = models.CharField(
        "通知类别", max_length=20, choices=TEMPLATE_CATEGORIES, db_index=True,
    )
    is_active = models.BooleanField("启用", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "通知模板"
        verbose_name_plural = verbose_name
        ordering = ["category", "code"]

    def __str__(self):
        return f"{self.code} ({self.name})"

    @property
    def supports_email(self):
        return bool(self.email_subject_template and self.email_body_template)


class Notification(models.Model):
    """通知主表。status=pending 的行即持久队列，由 notify_worker 轮询领取。

    发送侧状态（status）与接收侧状态（is_read）相互独立：
    - 发送侧：pending → sending → sent / failed（重试回 pending）；pending/sent(未读) 可 revoked；超时 expired
    - 接收侧：is_read/read_at 由用户阅读行为驱动
    """

    class Status(models.TextChoices):
        PENDING = "pending", "待发送"
        SENDING = "sending", "发送中"
        SENT = "sent", "发送成功"
        FAILED = "failed", "发送失败"
        REVOKED = "revoked", "已撤回"
        EXPIRED = "expired", "已过期"

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="notifications",
        verbose_name="接收用户",
        help_text="用户隔离根：查询/操作均按此字段过滤",
    )
    template = models.ForeignKey(
        NotificationTemplate,
        on_delete=models.PROTECT,
        related_name="notifications",
        verbose_name="通知模板",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="sent_notifications",
        verbose_name="触发人",
        help_text="管理员手动推送时记录；系统自动触发为空",
    )
    # 渲染后快照落库：模板后续修改不影响已发通知
    title = models.CharField("标题", max_length=200)
    content = models.TextField("内容")
    category = models.CharField(
        "通知类别", max_length=20, choices=TEMPLATE_CATEGORIES, db_index=True,
    )
    status = models.CharField(
        "发送状态", max_length=10, choices=Status.choices,
        default=Status.PENDING, db_index=True,
    )
    is_read = models.BooleanField("已读", default=False)
    read_at = models.DateTimeField("阅读时间", null=True, blank=True)
    priority = models.CharField("优先级", max_length=10, choices=PRIORITY_CHOICES, default="normal")
    # 幂等键：sha256(模板编码+接收者+业务上下文)，唯一约束兜底防重复落库
    dedup_key = models.CharField("幂等键", max_length=64, unique=True, null=True, blank=True)
    # 聚合键：窗口内同类触发合并计数，避免刷屏
    agg_key = models.CharField("聚合键", max_length=64, blank=True, default="", db_index=True)
    agg_count = models.IntegerField("聚合条数", default=1)
    # 公告批次：announce 批量推送的全部接收者副本共享同一批次，撤回时整批生效
    batch_id = models.CharField("公告批次", max_length=36, blank=True, default="", db_index=True)
    link = models.CharField("跳转链接", max_length=500, blank=True, default="")
    # 模板变量快照：供发送时渲染邮件渠道内容（DjangoJSONEncoder 兼容 date/Decimal 等）
    context_json = models.JSONField(
        "模板变量", default=dict, blank=True, encoder=DjangoJSONEncoder,
    )
    scheduled_at = models.DateTimeField("计划发送时间", default=timezone.now)
    expires_at = models.DateTimeField("过期时间", null=True, blank=True)
    retry_count = models.IntegerField("重试次数", default=0)
    next_retry_at = models.DateTimeField("下次重试时间", null=True, blank=True)
    last_error = models.TextField("最近错误", blank=True, default="")
    sent_at = models.DateTimeField("发送完成时间", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "通知"
        verbose_name_plural = verbose_name
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "-created_at"], name="idx_notif_recipient_time"),
            models.Index(fields=["status", "next_retry_at"], name="idx_notif_status_retry"),
            models.Index(fields=["recipient", "is_read"], name="idx_notif_recipient_read"),
            models.Index(fields=["recipient", "agg_key", "is_read"], name="idx_notif_agg"),
        ]

    def __str__(self):
        return f"{self.title} → {self.recipient} [{self.status}]"

    # 接收者可见范围：撤回/过期对用户隐藏（从未送达或已作废）
    RECIPIENT_VISIBLE_STATUS = ("pending", "sending", "sent", "failed")

    @property
    def status_badge_class(self):
        return {
            self.Status.PENDING: "info",
            self.Status.SENDING: "info",
            self.Status.SENT: "success",
            self.Status.FAILED: "danger",
            self.Status.REVOKED: "default",
            self.Status.EXPIRED: "default",
        }.get(self.status, "default")

    @property
    def can_revoke(self):
        """可撤回：待发送，或已发送且接收者未读。已读/已撤回/已过期不可撤。"""
        if self.status == self.Status.PENDING:
            return True
        return self.status == self.Status.SENT and not self.is_read


class NotificationPreference(models.Model):
    """用户通知偏好（每用户一行，OneToOne）。

    站内通知为系统基础能力始终开启；channel_prefs 控制各类别是否发邮件，
    如 {"task": {"email": false}}；缺省键视为开启。
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notify_prefs",
        verbose_name="用户",
    )
    channel_prefs = models.JSONField("渠道偏好", default=dict, blank=True)
    agg_window_minutes = models.IntegerField(
        "聚合降噪窗口(分钟)", default=5,
        help_text="同键通知在该窗口内合并计数；0 = 不聚合",
    )
    quiet_start = models.TimeField("免打扰开始", null=True, blank=True)
    quiet_end = models.TimeField("免打扰结束", null=True, blank=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "用户通知偏好"
        verbose_name_plural = verbose_name

    def __str__(self):
        return f"{self.user} 的通知偏好"

    def email_enabled(self, category):
        """指定类别是否发邮件（缺省开启）。"""
        per = (self.channel_prefs or {}).get(category, {})
        return bool(per.get("email", True))

    def in_quiet_hours(self, now_time=None):
        """当前是否处于免打扰时段。"""
        if not (self.quiet_start and self.quiet_end):
            return False
        now_time = now_time or timezone.localtime().time()
        return self.quiet_start <= now_time <= self.quiet_end


class NotificationDelivery(models.Model):
    """发送渠道日志：一条通知 × 一个渠道一行（重试时累计 attempts）。"""

    class ChannelStatus(models.TextChoices):
        SUCCESS = "success", "成功"
        FAILED = "failed", "失败"
        SKIPPED = "skipped", "已跳过"

    notification = models.ForeignKey(
        Notification,
        on_delete=models.CASCADE,
        related_name="deliveries",
        verbose_name="通知",
    )
    channel = models.CharField("发送渠道", max_length=10, choices=CHANNELS)
    status = models.CharField(
        "结果", max_length=10, choices=ChannelStatus.choices,
        default=ChannelStatus.SUCCESS,
    )
    detail = models.TextField("结果详情", blank=True, default="")
    attempts = models.IntegerField("尝试次数", default=1)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "发送渠道日志"
        verbose_name_plural = verbose_name
        ordering = ["notification", "channel"]
        constraints = [
            models.UniqueConstraint(
                fields=["notification", "channel"],
                name="uniq_delivery_notification_channel",
            )
        ]

    def __str__(self):
        return f"#{self.notification_id} {self.channel} {self.status}"
