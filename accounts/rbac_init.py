"""RBAC 初始化/同步逻辑（数据迁移与 sync_rbac 管理命令共用）。

所有函数接收 Role/Permission 模型类作为参数，
以便迁移中使用 apps.get_model 的历史模型版本。
"""

from .registry import BUILTIN_ROLES, all_permission_specs, builtin_role_codes


def sync_permissions(Permission):
    """幂等同步权限码：缺失则创建，不删除历史权限码。

    返回 (新建数, 现有总数)。
    """
    existing = {p.code for p in Permission.objects.all()}
    created = 0
    for code, module, action, name in all_permission_specs():
        if code not in existing:
            Permission.objects.create(code=code, module=module, action=action, name=name)
            created += 1
    return created, Permission.objects.count()


def ensure_builtin_roles(Role, Permission):
    """幂等创建/校准三个内置角色的权限集合（权限码集合强制对齐 registry）。

    返回 {role_code: 权限数}。
    """
    result = {}
    for code, (name, desc, _modules, _actions) in BUILTIN_ROLES.items():
        role, _created = Role.objects.get_or_create(
            code=code,
            defaults={"name": name, "description": desc, "is_builtin": True, "is_active": True},
        )
        # 内置角色名称/说明跟随注册表更新，但不覆盖管理员手工调整的启用状态
        if not role.is_builtin:
            role.is_builtin = True
        role.name = name
        role.description = desc
        role.save()
        codes = builtin_role_codes(code)
        perms = list(Permission.objects.filter(code__in=codes))
        role.permissions.set(perms)
        result[code] = len(perms)
    return result
