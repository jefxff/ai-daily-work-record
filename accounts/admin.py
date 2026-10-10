from django.contrib import admin

from .models import LoginLog, Permission, Role, UserProfile


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
