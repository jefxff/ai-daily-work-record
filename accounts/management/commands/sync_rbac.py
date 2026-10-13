"""幂等同步 RBAC 权限码与内置角色。

用法：python manage.py sync_rbac [--reset-builtins]
- 新模块登记到 accounts/registry.py 后执行；
- 重复执行安全：只补缺权限码，不删除、不影响自定义角色；
- 内置角色已存在时不覆盖其权限（界面手工调整不会被重置），
  如需强制对齐 registry 默认定义，加 --reset-builtins。
"""

from django.core.management.base import BaseCommand

from accounts.models import Permission, Role
from accounts.rbac_init import ensure_builtin_roles, sync_permissions


class Command(BaseCommand):
    help = "同步 RBAC 权限码并校准内置角色（幂等）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset-builtins",
            action="store_true",
            help="强制将内置角色（admin/staff/viewer）权限重置为 registry 默认定义",
        )

    def handle(self, *args, **options):
        created, total = sync_permissions(Permission)
        self.stdout.write(
            self.style.SUCCESS(f"权限码：新建 {created} 个，当前共 {total} 个")
        )
        reset = options["reset_builtins"]
        role_counts = ensure_builtin_roles(Role, Permission, reset=reset)
        for code, n in role_counts.items():
            suffix = "（已重置为默认）" if reset else "（未改动）"
            self.stdout.write(
                self.style.SUCCESS(f"内置角色 {code}：{n} 项权限{suffix}")
            )
