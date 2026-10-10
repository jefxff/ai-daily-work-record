"""幂等同步 RBAC 权限码与内置角色。

用法：python manage.py sync_rbac
- 新模块登记到 accounts/registry.py 后执行；
- 重复执行安全：只补缺权限码，不删除、不影响自定义角色。
"""

from django.core.management.base import BaseCommand

from accounts.models import Permission, Role
from accounts.rbac_init import ensure_builtin_roles, sync_permissions


class Command(BaseCommand):
    help = "同步 RBAC 权限码并校准内置角色（幂等）"

    def handle(self, *args, **options):
        created, total = sync_permissions(Permission)
        self.stdout.write(
            self.style.SUCCESS(f"权限码：新建 {created} 个，当前共 {total} 个")
        )
        role_counts = ensure_builtin_roles(Role, Permission)
        for code, n in role_counts.items():
            self.stdout.write(self.style.SUCCESS(f"内置角色 {code}：{n} 项权限"))
