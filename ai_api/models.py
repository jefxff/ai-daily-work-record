"""AI API 模块数据模型。

ai_api 的业务数据仍来自 records / samples / archive / knowledge_base 等 app，
通过 DRF 直接暴露接口；此文件仅承载「AI 配置」功能新增的单例配置模型
AiProviderConfig —— 用于保存周报 / 年度总结 AI 润色的接入参数。
"""

from django.db import models

from .crypto import decrypt_key, encrypt_key, looks_like_mask, mask_key


class AiProviderConfig(models.Model):
    """AI 接入配置。

    单例式设计保留给「文本润色」（purpose=polish），`current()` 行为与改造前
    完全一致（取最新一条）；Copilot 引入后按用途（purpose）区分模型：
    润色用小模型、问答用大模型、路由用更小的模型，各自独立配置。

    密钥属敏感字段，仅系统管理员（system.update）可在配置页查看与修改。
    """

    class Provider(models.TextChoices):
        MINIMAX = "minimax", "MiniMax"
        OLLAMA = "ollama", "本地 Ollama"
        OPENAI_COMPATIBLE = "openai_compatible", "自定义（OpenAI 兼容）"

    class Purpose(models.TextChoices):
        POLISH = "polish", "文本润色"
        CHAT = "chat", "对话问答"
        ROUTER = "router", "意图路由"
        EMBED = "embed", "向量化"

    # 各提供商未填地址时的默认 API 根地址（纯字符串键，避免枚举与字符串
    # 混用导致的 dict 查找失败）。
    DEFAULT_BASE_URL = {
        "minimax": "https://api.minimax.chat",
        "ollama": "http://127.0.0.1:11434",
        "openai_compatible": "",
    }

    # 各用途的超时默认值：润色短、问答长（多轮工具调用）、路由极短
    DEFAULT_TIMEOUT = {"polish": 30, "chat": 60, "router": 10, "embed": 20}

    purpose = models.CharField(
        "用途", max_length=16, choices=Purpose.choices,
        default=Purpose.POLISH, db_index=True,
        help_text="润色=周报/年度总结；对话问答=Copilot；意图路由/向量化为预留",
    )
    provider = models.CharField(
        "接入方式", max_length=32,
        choices=Provider.choices, default=Provider.MINIMAX,
    )
    base_url = models.CharField("API 地址", max_length=500, blank=True)
    api_key = models.CharField("API 密钥", max_length=500, blank=True)
    model = models.CharField("模型名称", max_length=200, blank=True)
    temperature = models.FloatField("温度", default=0.3)
    max_tokens = models.PositiveIntegerField("最大生成 Token", default=800)
    timeout = models.PositiveIntegerField("超时（秒）", default=30)
    is_local = models.BooleanField(
        "本地模型（数据不出内网）", default=False,
        help_text="勾选后视为数据不出内网，敏感模块才允许向该模型发送数据",
    )
    supports_tools = models.BooleanField(
        "支持原生函数调用", default=False,
        help_text="不勾选则使用受控 JSON 文本协议（本地小模型建议不勾选）",
    )
    daily_token_quota = models.PositiveIntegerField(
        "每日 token 上限（0=不限）", default=0,
    )
    is_enabled = models.BooleanField("启用 AI 润色", default=False)
    updated_at = models.DateTimeField("更新时间", auto_now=True)
    updated_by = models.CharField("最后修改人", max_length=150, blank=True)

    class Meta:
        verbose_name = "AI 配置"
        verbose_name_plural = "AI 配置"

    def __str__(self):
        return f"{self.get_provider_display()} / {self.model or '未指定模型'}"

    @classmethod
    def current(cls):
        """当前生效配置：单例取最新一条；不存在返回 None。

        保留原签名（周报润色等既有调用方不受影响），语义等价于
        ``for_purpose(Purpose.POLISH)``。
        """
        return cls.for_purpose(cls.Purpose.POLISH)

    @classmethod
    def for_purpose(cls, purpose):
        """按用途取配置。

        查找顺序：该用途下最新一条 → 全表最新一条 → None。
        第二级回退是为了兼容"只配了一条配置"的存量环境：只填了问答模型时，
        润色仍然可用，不会因为没建 polish 行而静默失效。
        """
        config = cls.objects.filter(purpose=purpose).order_by("-id").first()
        if config is not None:
            return config
        return cls.objects.order_by("-id").first()

    @classmethod
    def exact_for(cls, purpose):
        """严格按用途取最新一条，**不做跨用途回退**。

        配置页编辑某个用途时必须用这个：早期用 ``for_purpose`` 会让「对话问答」
        页显示出「文本润色」那一行，用户以为在改问答模型，其实改的是润色配置，
        而 AI 助手仍然跑在原来的云端模型上（表现为页面改不动、一直被拦外发）。
        """
        return cls.objects.filter(purpose=purpose).order_by("-id").first()

    @classmethod
    def enabled_candidates(cls, purpose):
        """某用途下所有启用中的配置，本地模型优先。

        Copilot 用它实现"本地优先、云端兜底"：先试本地（数据不出内网），
        本地未配置或调用失败再试云端（是否允许由 security 层按数据分级决定）。
        """
        rows = list(cls.objects.filter(purpose=purpose, is_enabled=True))
        if not rows:
            rows = list(cls.objects.filter(is_enabled=True))
        # 本地优先，其次按 id 倒序（新配置优先）
        return sorted(rows, key=lambda c: (0 if c.is_local else 1, -c.id))

    def effective_timeout(self):
        """当前用途的实际超时：显式配了非默认值就用它，否则按用途给默认值。"""
        default = self.DEFAULT_TIMEOUT.get(self.purpose, 30)
        return self.timeout if self.timeout and self.timeout != 30 else default

    def effective_base_url(self):
        """未显式填写地址时，回退到该提供商的默认地址。"""
        return (self.base_url or "").strip() or self.DEFAULT_BASE_URL.get(self.provider, "")

    # ---------- API 密钥透明加解密 ----------

    @property
    def api_key_plain(self) -> str:
        """解密后的明文密钥（供 client.py 实际调用 API 时使用）。"""
        return decrypt_key(self.api_key or "")

    @property
    def api_key_masked(self) -> str:
        """掩码显示（供配置页展示，如 'sk-...****'）。"""
        return mask_key(self.api_key or "")

    @property
    def api_key_is_masked(self) -> bool:
        """已存的值本身是不是掩码（历史 bug 的脏数据识别）。

        一旦为 True，说明密钥曾经被页面回填的掩码覆盖过，任何调用都会鉴权失败，
        配置页据此给出「请重新输入密钥」的提示。
        """
        return looks_like_mask(self.api_key_plain)

    def set_api_key(self, plaintext: str):
        """加密后存入 api_key 字段。空值原样存空字符串。

        掩码一律拒绝：页面把 ``api_key_masked`` 回填进表单后，若原样提交就会把
        掩码加密落库，此后 ``api_key_plain`` 返回的是掩码，模型鉴权必然失败。
        这里做成硬性拦截，任何调用方（含将来的批量导入）都无法绕过。
        """
        value = (plaintext or "").strip()
        if looks_like_mask(value):
            raise ValueError("密钥不能是掩码显示值，请填写完整的 API 密钥")
        self.api_key = encrypt_key(value)
