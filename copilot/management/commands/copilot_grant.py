"""给内置角色补齐「AI 助手」权限（只增不删）。

为什么不直接用 ``sync_rbac --reset-builtins``：那会把管理员在界面上
调整过的角色权限全部重置回默认，副作用太大。本命令只补 ``copilot.read``，
其它权限一律不动，可重复执行。

    python manage.py copilot_grant            # 内置角色
    python manage.py copilot_grant --role 销售  # 指定角色编码，可多次
"""

from django.core.management.base import BaseCommand

from accounts.models import Permission, Role

BUILTIN = ('admin', 'staff', 'viewer')
PERMISSION_CODE = 'copilot.read'


class Command(BaseCommand):
    help = '给角色补齐 AI 助手使用权限（只增不删，幂等）'

    def add_arguments(self, parser):
        parser.add_argument('--role', action='append', default=[],
                            help='额外指定的角色编码，可重复')

    def handle(self, *args, **options):
        permission = Permission.objects.filter(code=PERMISSION_CODE).first()
        if permission is None:
            self.stderr.write(
                f'权限码 {PERMISSION_CODE} 不存在，请先执行 python manage.py sync_rbac')
            return

        codes = list(dict.fromkeys(list(BUILTIN) + list(options['role'])))
        for code in codes:
            role = Role.objects.filter(code=code).first()
            if role is None:
                self.stdout.write(f'角色 {code} 不存在，跳过')
                continue
            if role.permissions.filter(pk=permission.pk).exists():
                self.stdout.write(f'角色 {code} 已有 {PERMISSION_CODE}，跳过')
                continue
            role.permissions.add(permission)
            self.stdout.write(self.style.SUCCESS(f'角色 {code} 已授予 {PERMISSION_CODE}'))
