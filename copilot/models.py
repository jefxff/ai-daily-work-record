"""Copilot 数据模型。

设计要点：
1. 会话、消息、执行单元（run）三层分离——run 是可轮询的异步任务，
   消息是最终沉淀，避免"轮询接口顺带写消息"造成的重复与竞态；
2. 工具调用单独成表且**被拒绝的调用也落库**，这是发现"模型试图越权"
   的唯一途径；
3. 外发审计（CopilotEgressLog）独立于操作留痕——留痕面向业务审计，
   外发日志面向合规问询（谁把哪些模块的数据发给了哪家模型）。
"""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class CopilotPolicy(models.Model):
    """全局策略（单例）。

    放在数据库而非 settings 的原因：需要管理员在界面上按模块放行外发、
    调限流阈值，改 settings 要重启进程，运维成本高。
    """

    allow_cloud_modules = models.JSONField(
        '允许外发云端的模块', default=list, blank=True,
        help_text='填写 RBAC 模块 key（如 insurance）；未列出的敏感模块禁止发往云端模型',
    )
    mask_enabled = models.BooleanField('启用脱敏', default=True)
    rate_limit_per_10min = models.PositiveIntegerField('每 10 分钟请求上限', default=20)
    rate_limit_per_day = models.PositiveIntegerField('每日请求上限', default=200)
    retention_days = models.PositiveIntegerField('会话保留天数', default=90)
    # 外发日志是合规证据，不能随会话一起删：会话清了，"谁把什么发给了哪家
    # 模型"仍要能回答，所以单独一个更长的保留期。
    egress_retention_days = models.PositiveIntegerField('外发日志保留天数', default=180)
    max_tool_rounds = models.PositiveIntegerField('单次问答最多工具轮次', default=3)
    max_total_tokens = models.PositiveIntegerField('单次问答 token 预算', default=6000)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    updated_by = models.CharField('最后修改人', max_length=150, blank=True)

    class Meta:
        verbose_name = 'AI 助手策略'
        verbose_name_plural = 'AI 助手策略'

    def __str__(self):
        return 'AI 助手策略'

    @classmethod
    def current(cls):
        """取策略单例；不存在时返回一个未保存的默认实例（不落库）。"""
        return cls.objects.order_by('-id').first() or cls()

    def cloud_allowed(self, module_key):
        return module_key in (self.allow_cloud_modules or [])


class CopilotConversation(models.Model):
    """一次会话（一个对话窗口）。"""

    class Status(models.TextChoices):
        ACTIVE = 'active', '进行中'
        ARCHIVED = 'archived', '已归档'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='copilot_conversations', verbose_name='归属用户',
    )
    title = models.CharField('标题', max_length=120, blank=True)
    status = models.CharField('状态', max_length=16, choices=Status.choices,
                              default=Status.ACTIVE)
    # 会话级"槽位记忆"：上一轮解析出的模块/关键词/状态/时间口径。
    # 追问（"那这个客户呢""上个月的记录呢"）先看槽位再看正文，避免
    # MAX_HISTORY 之外就断片。换话题时必须清空，否则张冠李戴。
    slots = models.JSONField('槽位记忆', default=dict, blank=True)
    token_total = models.PositiveIntegerField('累计 token', default=0)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    last_active_at = models.DateTimeField('最后活跃', default=timezone.now)
    # 软删时间：用户点删除只是归档，30 天内可从数据库层面物理清理，
    # 期间误删还能由管理员恢复。
    archived_at = models.DateTimeField('归档时间', null=True, blank=True)

    class Meta:
        verbose_name = 'AI 会话'
        verbose_name_plural = 'AI 会话'
        ordering = ['-last_active_at']
        indexes = [models.Index(fields=['user', '-last_active_at'])]

    def __str__(self):
        return f'{self.user} · {self.title or "新会话"}'


class CopilotMessage(models.Model):
    """会话中的一条消息（用户提问 / 助手回答 / 工具结果摘要）。"""

    class Role(models.TextChoices):
        USER = 'user', '用户'
        ASSISTANT = 'assistant', '助手'
        TOOL = 'tool', '工具'
        SYSTEM = 'system', '系统'

    conversation = models.ForeignKey(
        CopilotConversation, on_delete=models.CASCADE,
        related_name='messages', verbose_name='会话',
    )
    role = models.CharField('角色', max_length=16, choices=Role.choices)
    content = models.TextField('内容', blank=True)
    tool_calls = models.JSONField('工具调用', default=list, blank=True)
    refs = models.JSONField('引用', default=list, blank=True)
    provider = models.CharField('提供方', max_length=32, blank=True)
    model = models.CharField('模型', max_length=120, blank=True)
    prompt_tokens = models.PositiveIntegerField('输入 token', default=0)
    completion_tokens = models.PositiveIntegerField('输出 token', default=0)
    latency_ms = models.PositiveIntegerField('耗时(ms)', default=0)
    error_code = models.CharField('错误码', max_length=32, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = 'AI 消息'
        verbose_name_plural = 'AI 消息'
        ordering = ['id']
        indexes = [models.Index(fields=['conversation', 'id'])]

    def __str__(self):
        return f'{self.role}: {self.content[:30]}'


class CopilotRun(models.Model):
    """一次问答的执行单元（可轮询）。"""

    class Status(models.TextChoices):
        PENDING = 'pending', '排队中'
        RUNNING = 'running', '执行中'
        SUCCEEDED = 'succeeded', '已完成'
        FAILED = 'failed', '失败'
        TIMEOUT = 'timeout', '超时'
        CANCELLED = 'cancelled', '已取消'
        INTERRUPTED = 'interrupted', '中断'

    TERMINAL = {
        Status.SUCCEEDED, Status.FAILED, Status.TIMEOUT,
        Status.CANCELLED, Status.INTERRUPTED,
    }

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        CopilotConversation, on_delete=models.CASCADE,
        related_name='runs', verbose_name='会话',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='copilot_runs', verbose_name='发起人',
    )
    status = models.CharField('状态', max_length=16, choices=Status.choices,
                              default=Status.PENDING)
    question = models.TextField('问题')
    answer = models.TextField('回答', blank=True)
    refs = models.JSONField('引用', default=list, blank=True)
    steps = models.JSONField('执行步骤', default=list, blank=True)
    provider = models.CharField('提供方', max_length=32, blank=True)
    model = models.CharField('模型', max_length=120, blank=True)
    total_tokens = models.PositiveIntegerField('消耗 token', default=0)
    attempts = models.PositiveSmallIntegerField('尝试次数', default=0)
    error_code = models.CharField('错误码', max_length=32, blank=True)
    error_message = models.CharField('错误信息', max_length=500, blank=True)
    heartbeat_at = models.DateTimeField('心跳', default=timezone.now)
    deadline_at = models.DateTimeField('截止时间', default=timezone.now)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    finished_at = models.DateTimeField('完成时间', null=True, blank=True)

    class Meta:
        verbose_name = 'AI 执行'
        verbose_name_plural = 'AI 执行'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'heartbeat_at']),
            models.Index(fields=['user', '-created_at']),
        ]
        constraints = [
            # 同一会话同时只允许一个进行中的 run：重复提交返回已有 run，
            # 避免用户连点产生多份并发调用（也保护 backend 配额）。
            models.UniqueConstraint(
                fields=['conversation'],
                condition=models.Q(status__in=('pending', 'running')),
                name='uniq_copilot_active_run_per_conversation',
            ),
        ]

    def __str__(self):
        return f'{self.status} · {self.question[:30]}'

    @property
    def is_terminal(self):
        return self.status in self.TERMINAL

    def touch(self):
        """更新心跳（执行器每完成一步调用一次）。"""
        self.heartbeat_at = timezone.now()
        CopilotRun.objects.filter(pk=self.pk).update(heartbeat_at=self.heartbeat_at)


class CopilotToolCall(models.Model):
    """一次工具调用（含被拒绝的）。"""

    run = models.ForeignKey(
        CopilotRun, on_delete=models.CASCADE,
        related_name='tool_calls', verbose_name='执行',
    )
    conversation = models.ForeignKey(
        CopilotConversation, on_delete=models.CASCADE,
        related_name='tool_calls', verbose_name='会话',
    )
    tool_name = models.CharField('工具', max_length=64)
    args = models.JSONField('参数', default=dict, blank=True)
    permitted = models.BooleanField('是否放行', default=False)
    denied_reason = models.CharField('拒绝原因', max_length=120, blank=True)
    row_count = models.PositiveIntegerField('返回行数', default=0)
    latency_ms = models.PositiveIntegerField('耗时(ms)', default=0)
    result_digest = models.CharField('结果摘要', max_length=32, blank=True)
    truncated = models.BooleanField('是否截断', default=False)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = 'AI 工具调用'
        verbose_name_plural = 'AI 工具调用'
        ordering = ['id']
        indexes = [models.Index(fields=['conversation', '-created_at'])]

    def __str__(self):
        flag = 'OK' if self.permitted else 'DENY'
        return f'{flag} {self.tool_name}[{self.row_count}]'


class CopilotEgressLog(models.Model):
    """数据外发审计：把哪个模块的多少行数据发给了哪家模型。"""

    conversation = models.ForeignKey(
        CopilotConversation, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='egress_logs', verbose_name='会话',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name='发起人',
    )
    provider = models.CharField('提供方', max_length=32)
    model = models.CharField('模型', max_length=120, blank=True)
    is_local = models.BooleanField('本地模型', default=False)
    module_keys = models.JSONField('涉及模块', default=list, blank=True)
    row_count = models.PositiveIntegerField('数据行数', default=0)
    fields_masked = models.JSONField('已脱敏字段', default=list, blank=True)
    created_at = models.DateTimeField('时间', auto_now_add=True)

    class Meta:
        verbose_name = 'AI 数据外发日志'
        verbose_name_plural = 'AI 数据外发日志'
        ordering = ['-created_at']
        indexes = [models.Index(fields=['-created_at'])]
        # 只允许超管在 admin 查看；不做业务入口

    def __str__(self):
        return f'{self.provider}/{self.model} · {",".join(self.module_keys or [])}'


class CopilotEvalCase(models.Model):
    """评估集用例：换模型 / 改提示词后的回归基线。"""

    class Mode(models.TextChoices):
        LOCAL = 'local', '本地链路'
        MODEL = 'model', '模型链路'

    question = models.TextField('问题')
    expected_tools = models.JSONField('期望工具', default=list, blank=True)
    expected_keywords = models.JSONField('答案要点', default=list, blank=True)
    must_not_contain = models.JSONField('禁止出现', default=list, blank=True)
    module_scope = models.CharField('模块范围', max_length=64, blank=True)
    # 多轮用例的前置问题：先问这一句建立上下文，再问 question。
    # 空表示单轮用例。
    prelude = models.CharField('前置问题', max_length=200, blank=True)
    # 本地链路用例与模型链路用例分开跑：前者不进 --with-model（那批只该
    # 验证"模型兜不住的长尾"），否则每次评估都白烧 token。
    mode = models.CharField('链路', max_length=8, choices=Mode.choices,
                            default=Mode.MODEL)
    active = models.BooleanField('启用', default=True)
    # 用例来源：人工挑的长尾问法、或由点踩反馈一键转成的草稿
    source_feedback = models.ForeignKey(
        'copilot.CopilotFeedback', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='eval_cases', verbose_name='来源反馈',
    )
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = 'AI 评估用例'
        verbose_name_plural = 'AI 评估用例'
        ordering = ['id']

    def __str__(self):
        return self.question[:40]


class CopilotFeedback(models.Model):
    """用户对回答的反馈（踩 → 可一键加入评估集）。"""

    class Reason(models.TextChoices):
        WRONG_MODULE = 'wrong_module', '答错模块'
        WRONG_NUMBER = 'wrong_number', '数字不对'
        MISSING = 'missing', '没有这条'
        SLOW = 'slow', '太慢'
        OTHER = 'other', '其它'

    message = models.ForeignKey(
        CopilotMessage, on_delete=models.CASCADE,
        related_name='feedbacks', verbose_name='消息',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        verbose_name='用户',
    )
    vote = models.SmallIntegerField('评价', default=0, choices=[(-1, '踩'), (1, '赞')])
    reason = models.CharField('原因', max_length=20, choices=Reason.choices, blank=True)
    comment = models.CharField('备注', max_length=500, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        verbose_name = 'AI 反馈'
        verbose_name_plural = 'AI 反馈'
        unique_together = [('message', 'user')]

    def __str__(self):
        return f'{self.vote} · {self.message_id}'
