"""AI API 模块数据模型。

ai_api 的业务数据仍来自 records / samples / archive / knowledge_base 等 app，
通过 DRF 直接暴露接口；此文件仅承载「AI 配置」功能新增的单例配置模型
AiProviderConfig —— 用于保存周报 / 年度总结 AI 润色的接入参数。
"""

from django.db import models

from .crypto import decrypt_key, encrypt_key, mask_key


class AiProviderConfig(models.Model):
    """AI 接入配置（周报 / 年度总结润色）。

    单例式：全站仅一条当前生效配置，取 ``current()``。密钥属敏感字段，
    仅系统管理员（system.update）可在配置页查看与修改。

    api_key 字段数据库存 Fernet 加密值（'fernet:' 前缀），明文
    由以下 property / setter 透明处理：
    - ``api_key_plain``  — 解密后的明文（供 client.py 实际调用）
    - ``api_key_masked`` — 掩码（供配置页展示，如 'sk-...****'）
    - ``set_api_key(s)`` — 加密后落库（供保存逻辑调用）
    """

    class Provider(models.TextChoices):
        MINIMAX = "minimax", "MiniMax"
        OLLAMA = "ollama", "本地 Ollama"
        OPENAI_COMPATIBLE = "openai_compatible", "自定义（OpenAI 兼容）"

    # 各提供商未填地址时的默认 API 根地址（纯字符串键，避免枚举与字符串
    # 混用导致的 dict 查找失败）。
    DEFAULT_BASE_URL = {
        "minimax": "https://api.minimax.chat",
        "ollama": "http://127.0.0.1:11434",
        "openai_compatible": "",
    }

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
        """当前生效配置：单例取最新一条；不存在返回 None。"""
        return cls.objects.order_by("-id").first()

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

    def set_api_key(self, plaintext: str):
        """加密后存入 api_key 字段。空值原样存空字符串。"""
        self.api_key = encrypt_key((plaintext or "").strip())
