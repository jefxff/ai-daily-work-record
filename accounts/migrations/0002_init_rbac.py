"""幂等初始化 RBAC 权限码与三个内置角色。"""

from django.db import migrations


def init_rbac(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    Permission = apps.get_model("accounts", "Permission")
    # 延迟导入，确保迁移自包含于 accounts app（registry 为纯 Python 常量模块）
    from accounts.rbac_init import ensure_builtin_roles, sync_permissions

    sync_permissions(Permission)
    ensure_builtin_roles(Role, Permission)


def remove_builtin_rbac(apps, schema_editor):
    """回滚：仅删除内置角色与本期初始化的权限码。"""
    Role = apps.get_model("accounts", "Role")
    Permission = apps.get_model("accounts", "Permission")
    from accounts.registry import BUILTIN_ROLES, all_permission_codes

    Role.objects.filter(code__in=list(BUILTIN_ROLES.keys())).delete()
    Permission.objects.filter(code__in=all_permission_codes()).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(init_rbac, remove_builtin_rbac),
    ]
