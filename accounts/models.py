"""RBAC 模型：Role（角色）- Permission（权限）- User（用户，多对多）。

权限码格式：<module>.<action>，如 samples.create；模块清单见 registry.py。
用户可绑定多个启用中的角色，有效权限取并集；is_superuser 恒为全权限。
"""

from django.conf import settings
from django.db import models


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
