"""权限判定服务 —— 全站唯一的 RBAC 判定入口。

缓存生命周期绑定 user 实例（每请求由 AuthenticationMiddleware 重建），
因此角色/权限变更在下次请求必然生效，不存在跨请求陈旧缓存。
"""

from .models import Role
from .registry import MODULE_KEYS, MODULE_MAP, permission_code

_SUPERUSER = object()  # 超管哨兵：懒求值全集


def _codes(user):
    """返回用户有效权限码集合；超管返回全集。"""
    if not getattr(user, "is_authenticated", False):
        return frozenset()
    if user.is_superuser:
        from .registry import all_permission_codes

        return set(all_permission_codes())
    cached = getattr(user, "_rbac_codes_cache", None)
    if cached is not None:
        return cached
    # 2 条 SQL：角色 + 角色权限预取（遍历必须用 .all() 才命中预取缓存）
    roles = Role.objects.filter(users=user, is_active=True).prefetch_related("permissions")
    codes = set()
    for role in roles:
        codes.update(perm.code for perm in role.permissions.all())
    user._rbac_codes_cache = codes
    return codes


def has_perm(user, module, action):
    """判定用户是否拥有某模块某动作权限。

    超管恒真；匿名恒假；模块/动作非法恒假。
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return True
    if module not in MODULE_MAP or action not in MODULE_MAP[module]["actions"]:
        return False
    return permission_code(module, action) in _codes(user)


def module_actions(user, module):
    """用户在某模块拥有的动作集合。"""
    if not getattr(user, "is_authenticated", False):
        return set()
    if user.is_superuser and module in MODULE_MAP:
        return set(MODULE_MAP[module]["actions"])
    return {a for a in MODULE_MAP.get(module, {}).get("actions", []) if has_perm(user, module, a)}


def visible_modules(user):
    """用户可见模块 key 列表（拥有该模块任意动作权限即可见），按 registry 顺序。"""
    if not getattr(user, "is_authenticated", False):
        return []
    if user.is_superuser:
        return list(MODULE_KEYS)
    codes = _codes(user)
    return [
        key
        for key in MODULE_KEYS
        if any(permission_code(key, a) in codes for a in MODULE_MAP[key]["actions"])
    ]


def can_view_module(user, module):
    """模块可见度：拥有该模块任意动作权限（或超管）。"""
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return module in MODULE_MAP
    return bool(module_actions(user, module))


def get_user_roles_display(user):
    """用户当前启用角色中文名列表（用于顶栏展示）。"""
    if not getattr(user, "is_authenticated", False):
        return []
    if user.is_superuser:
        return ["超级管理员"]
    return list(
        Role.objects.filter(users=user, is_active=True).order_by("code").values_list("name", flat=True)
    )


def get_display_name(user):
    """用户展示名：Profile.姓名 > 用户名。"""
    if not getattr(user, "is_authenticated", False):
        return ""
    profile = getattr(user, "profile", None)
    if profile and profile.real_name:
        return profile.real_name
    return user.username
