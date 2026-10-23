"""RBAC 模型：Role（角色）- Permission（权限）- User（用户，多对多）。

权限码格式：<module>.<action>，如 samples.create；模块清单见 registry.py。
用户可绑定多个启用中的角色，有效权限取并集；is_superuser 恒为全权限。
"""

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

# 长期 Token：随机串字节数与有效期（天）
TOKEN_KEY_BYTES = 48
TOKEN_TTL_DAYS = 365


def _hash_token_key(raw_key: str) -> str:
    """Token key 哈希：sha256(明文 + SECRET_KEY salt)。

    入库后明文**不再保存**：Token 是给外部 Agent 的长期免交互凭据，明文落库意味着
    任何能读数据库的途径（只读副本、sqlite 文件、本系统自己的备份包）都等于拿到
    一批可直接冒用的凭据。校验只在认证时比对哈希，明文仅在签发响应里出现一次。
    """
    secret = settings.SECRET_KEY.encode("utf-8")
    return hashlib.sha256(raw_key.encode("utf-8") + secret).hexdigest()


class Permission(models.Model):
    """细粒度操作权限：模块 × CRUD 动作，一行一个权限码。"""

    code = models.CharField("权限码", max_length=100, unique=True, db_index=True)
    module = models.CharField("模块", max_length=50, db_index=True)
    action = models.CharField("动作", max_length=10)
    name = models.CharField("权限名称", max_length=100)

    class Meta:
        verbose_name = "权限"
        verbose_name_plural = verbose_name
        constraints = [
            models.UniqueConstraint(
                fields=["module", "action"], name="uniq_permission_module_action"
            )
        ]
        ordering = ["module", "action"]

    def __str__(self):
        return self.name


class Role(models.Model):
    """角色：权限的集合，可分配给多个用户。"""

    code = models.CharField("角色编码", max_length=50, unique=True)
    name = models.CharField("角色名称", max_length=50)
    description = models.TextField("说明", blank=True)
    is_builtin = models.BooleanField("内置角色", default=False)
    is_active = models.BooleanField("启用", default=True)
    permissions = models.ManyToManyField(
        Permission, verbose_name="权限", related_name="roles", blank=True
    )
    users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        verbose_name="用户",
        related_name="rbac_roles",
        blank=True,
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "角色"
        verbose_name_plural = verbose_name
        ordering = ["-is_builtin", "code"]

    def __str__(self):
        return self.name

    @property
    def user_count(self):
        return self.users.count()


class UserProfile(models.Model):
    """用户档案：auth.User 的扩展资料（一对一）。"""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name="用户",
        on_delete=models.CASCADE,
        related_name="profile",
    )
    real_name = models.CharField("姓名", max_length=50, blank=True)
    department = models.CharField("部门", max_length=100, blank=True)
    position = models.CharField("岗位", max_length=100, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "用户档案"
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.real_name or self.user.username


class LoginLog(models.Model):
    """登录/注册审计日志（只增不改，界面层不提供删除）。"""

    class LoginType(models.TextChoices):
        LOGIN = "login", "登录"
        REGISTER = "register", "注册申请"
        LOGOUT = "logout", "登出"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="用户",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="login_logs",
    )
    username = models.CharField("用户名", max_length=150, db_index=True)
    ip = models.GenericIPAddressField("IP地址", null=True, blank=True)
    user_agent = models.CharField("浏览器标识", max_length=400, blank=True)
    login_type = models.CharField(
        "类型", max_length=10, choices=LoginType.choices, default=LoginType.LOGIN, db_index=True
    )
    success = models.BooleanField("是否成功", default=True, db_index=True)
    reason = models.CharField("失败原因", max_length=200, blank=True)
    created_at = models.DateTimeField("时间", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "登录日志"
        verbose_name_plural = verbose_name
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.username} {'成功' if self.success else '失败'} {self.created_at:%Y-%m-%d %H:%M}"


class ApiToken(models.Model):
    """长期 Token：供外部 Agent 以请求头 ``Authorization: Token <key>`` 免交互校验身份。

    与 DRF 内置 authtoken（每用户仅一枚、无过期、无历史）不同，本模型保留历史记录、
    带过期时间，便于管理端审计。

    安全约束（审计 P1-2）：明文 key **不落库**。签发时哈希进 ``key_hash``、
    前缀进 ``key_prefix``，明文只在签发响应里回显一次；认证只可能按哈希比对。
    本系统每天产出含全库数据的备份包，明文入库等于把长期凭据一起打包送出去。
    """

    class Status(models.TextChoices):
        ACTIVE = "active", "有效"
        EXPIRED = "expired", "已过期"
        REVOKED = "revoked", "已撤销"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="用户",
        on_delete=models.CASCADE,
        related_name="api_tokens",
    )
    key_prefix = models.CharField(
        "Token 前缀",
        max_length=16,
        default="",
        blank=True,
        help_text="明文 key 的前几位，仅用于用户在历史列表里辨认是哪一枚 Token",
    )
    key_hash = models.CharField(
        "Token 哈希",
        max_length=64,
        default="",
        db_index=True,
        help_text="sha256(key + SECRET_KEY)，认证时唯一比对字段；明文不落库，仅在签发响应里回显一次",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    expires_at = models.DateTimeField("过期时间", null=True, blank=True)
    is_active = models.BooleanField("启用", default=True, db_index=True)
    created_ip = models.GenericIPAddressField("创建IP", null=True, blank=True)
    password_fingerprint = models.CharField(
        "签发时密码指纹",
        max_length=128,
        blank=True,
        default="",
        help_text="签发时的 user.password 哈希；改密或管理员重置后指纹不一致即视为失效",
    )

    class Meta:
        verbose_name = "长期 Token"
        verbose_name_plural = verbose_name
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "-created_at"], name="apitoken_user_created_idx"),
        ]

    def __str__(self):
        return f"{self.user.get_username()} · {self.created_at:%Y-%m-%d %H:%M}"

    @property
    def is_expired(self):
        return self.expires_at is not None and self.expires_at <= timezone.now()

    @property
    def password_changed(self):
        """签发后用户密码是否已变更（自助改密或管理员重置）。

        指纹在签发时写入；与当前密码哈希不一致，说明密码已变，该 Token 应随旧密码
        一并失效。空指纹视为未绑定（兼容历史数据），不据此判失效。
        """
        fp = self.password_fingerprint
        return bool(fp) and fp != self.user.password

    @property
    def is_valid(self):
        """当前是否可用于鉴权（启用、未过期、且密码未被改动）。"""
        return self.is_active and not self.is_expired and not self.password_changed

    @property
    def status_code(self):
        if not self.is_active:
            return self.Status.REVOKED
        if self.password_changed:
            return self.Status.REVOKED
        if self.is_expired:
            return self.Status.EXPIRED
        return self.Status.ACTIVE

    @property
    def status_text(self):
        return self.Status(self.status_code).label

    @property
    def masked_key(self):
        """历史列表展示用的掩码值。明文不落库，这里只能给前缀。"""
        return f"{self.key_prefix}…" if self.key_prefix else "（前缀缺失）"

    @classmethod
    def issue(cls, user, *, ip=None, ttl_days=TOKEN_TTL_DAYS):
        """为用户签发一枚新的长期 Token（不撤销既有 Token，保留完整历史）。

        返回 Token 对象，明文字 **只挂在实例的 ``raw_key`` 属性上**（不入库）。
        调用方必须在唯一一次响应中展示它——页面历史列表此后只能看到前缀。
        """
        raw_key = secrets.token_urlsafe(TOKEN_KEY_BYTES)
        token = cls.objects.create(
            user=user,
            key_prefix=raw_key[:12],
            key_hash=_hash_token_key(raw_key),
            expires_at=timezone.now() + timedelta(days=ttl_days),
            created_ip=ip,
            # 记录签发时的密码指纹：改密/重置后据此令本 Token 失效
            password_fingerprint=user.password or "",
        )
        # 明文只在本次响应里使用；绝不能 save()（save 会把内存属性写进 key_hash 之外的字段，
        # 而 raw_key 本身不是模型字段，不会被持久化，但也不要给它这个机会）
        token.raw_key = raw_key
        return token
