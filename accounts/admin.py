from django.contrib import admin

from .models import LoginLog, Permission, Role, UserProfile


def _superuser_only_has_permission(request):
    """审计 SEC-09：Admin 只允许超管进入。

    Django 默认 is_staff 即可登录，而项目业务权限由 registry 的模块粒度控制；
    允许普通 staff 进 Admin 等于给他们一条绕过 RBAC 的读写通道。
    """
    user = getattr(request, "user", None)
    return bool(user and user.is_active and user.is_superuser)


# 覆盖实例方法而不是 fork AdminSite：保留所有原有注册与静态资源行为
admin.site.has_permission = _superuser_only_has_permission


@admin.register(Permission)
class PermissionAdmin(admin.ModelAdmin):
    list_display = ("code", "module", "action", "name")
    list_filter = ("module", "action")
    search_fields = ("code", "name")


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_builtin", "is_active", "user_count")
    list_filter = ("is_builtin", "is_active")
    search_fields = ("code", "name")
    filter_horizontal = ("permissions", "users")


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "real_name", "department", "position")
    search_fields = ("user__username", "real_name", "department")


@admin.register(LoginLog)
class LoginLogAdmin(admin.ModelAdmin):
    list_display = ("username", "login_type", "success", "ip", "created_at")
    list_filter = ("success", "login_type", "created_at")
    search_fields = ("username", "ip")
