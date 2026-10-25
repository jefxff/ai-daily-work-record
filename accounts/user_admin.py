"""用户/角色管理的共享规则与写操作（页面视图与 DRF API 共用一套）。

为什么需要这一层：代码审查发现同一批规则在 ``accounts/views_admin.py``（服务端
渲染页面）与 ``ai_api/api_admin.py``（DRF）里各写一遍 —— 超管保护、``is_staff``
仅超管可授、密码策略、角色权限码校验、内置角色锁定。两份实现当时语义一致，但任何
一侧改动都会造成「页面允许、API 拒绝」这类静默不一致（本项目已出现过同类问题：
备份包后缀校验散落 5 处，导致「下载能过、恢复不能过」）。

与 ``accounts/services.py`` 的分工：那个模块是**RBAC 权限判定**（只读、带缓存），
本模块是**账户与角色的写操作**（含校验与落库）。两者互不依赖，避免循环导入。

约定：
- 这里只放规则与写入，不放 HTTP 逻辑（消息提示、状态码、序列化）；
- 校验失败统一抛 ``AccountRuleError``，由调用方决定转成表单错误、422 还是 DRF
  ``ValidationError``；
- 留痕摘要仍由调用方写：页面端与 API 端的措辞、``[API]`` 前缀是有意区分的，
  不属于重复规则。
"""

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction

from .models import Permission, Role, UserProfile
from .registry import all_permission_codes


class AccountRuleError(Exception):
    """账户/角色规则校验失败。调用方自行决定呈现方式（表单错误 / 422 / DRF）。"""


# ============================== 用户：边界与属性 ==============================

def may_manage_user(actor, target) -> bool:
    """非超级管理员不得操作超级管理员账号（防越权降级其余超管）。"""
    return not (
        getattr(target, "is_superuser", False)
        and not getattr(actor, "is_superuser", False)
    )


def check_can_manage_user(actor, target) -> None:
    """``may_manage_user`` 的抛异常版本。"""
    if not may_manage_user(actor, target):
        raise AccountRuleError("非超级管理员不能操作超管账号。")


def can_grant_staff(actor) -> bool:
    """``is_staff`` 决定能否进入 Django Admin，只允许超级管理员授予/撤销。"""
    return bool(getattr(actor, "is_superuser", False))


def validate_new_password(password, user=None) -> None:
    """按 ``settings.AUTH_PASSWORD_VALIDATORS`` 校验密码强度。

    传入 ``user`` 才能让相似度校验器比对「用户名/邮箱」，因此口径比页面表单原先
    「不传 user」的校验更严格 —— 有意收紧，页面与 API 现在完全一致。
    """
    if not password:
        raise AccountRuleError("新密码不能为空。")
    try:
        validate_password(password, user)
    except DjangoValidationError as exc:
        raise AccountRuleError("；".join(exc.messages)) from exc


# ============================== 用户：写操作 ==============================

def create_user_account(*, username, password, email="", is_active=True,
                        profile_data=None, roles=None) -> User:
    """创建用户 + 档案 + 角色挂载。

    初始密码必填（页面与 API 同一约定）；档案三字段缺省记空串，保证后续
    ``user.profile`` 一定有对象（避免模板/序列化器取空报错）。
    """
    profile_data = profile_data or {}
    user = User.objects.create_user(
        username=username,
        password=password,
        email=email or "",
        is_active=bool(is_active),
    )
    UserProfile.objects.create(
        user=user,
        real_name=profile_data.get("real_name") or "",
        department=profile_data.get("department") or "",
        position=profile_data.get("position") or "",
    )
    if roles is not None:
        user.rbac_roles.set(roles)
    return user


def update_user_account(actor, target, *, fields=None, profile_data=None,
                        roles=None) -> User:
    """更新用户基础字段 / 档案 / 角色。

    ``is_staff`` 只允许超级管理员修改；非超管提交的该字段直接丢弃（而不是报错），
    与页面端「非超管编辑表单里根本不渲染该项」的效果一致。
    """
    fields = dict(fields or {})
    if "is_staff" in fields and not can_grant_staff(actor):
        fields.pop("is_staff")
    for attr, value in fields.items():
        setattr(target, attr, value)
    target.save()
    if profile_data is not None:
        profile = getattr(target, "profile", None) or UserProfile.objects.create(user=target)
        for attr, value in profile_data.items():
            setattr(profile, attr, value)
        profile.save()
    if roles is not None:
        target.rbac_roles.set(roles)
    return target


def set_user_active(actor, target, active, *, forbid_self_disable=True) -> User:
    """启用/停用账号（审核通过、拒绝注册也走这里，只是留痕动作名不同）。"""
    check_can_manage_user(actor, target)
    if forbid_self_disable and not active and target.pk == getattr(actor, "pk", None):
        raise AccountRuleError("不能停用当前登录账号。")
    target.is_active = bool(active)
    target.save(update_fields=["is_active"])
    return target


def reset_user_password(actor, target, new_password) -> User:
    """管理员重置密码：越权校验 + 强度校验 + 落库。"""
    check_can_manage_user(actor, target)
    validate_new_password(new_password, target)
    target.set_password(new_password)
    # 密码指纹机制会让该用户其它会话在下次请求时被强制登出
    target.save(update_fields=["password"])
    return target


# ============================== 角色：规则与写操作 ==============================

def check_role_code_available(code, exclude_pk=None) -> None:
    """角色编码唯一（大小写不敏感，与页面端 ``code__iexact`` 一致）。"""
    qs = Role.objects.filter(code__iexact=(code or "").strip())
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    if qs.exists():
        raise AccountRuleError("角色编码已存在。")


def save_role_permissions(role, raw_codes) -> int:
    """校验并整体替换角色权限，返回生效的权限码数量。

    非法权限码在**任何写入之前**抛出，避免「权限删了一半才发现有非法码」。
    """
    valid = set(all_permission_codes())
    chosen = {c for c in (raw_codes or []) if c}
    illegal = chosen - valid
    if illegal:
        raise AccountRuleError(
            f"检测到非法权限码，已拒绝保存：{', '.join(sorted(illegal))}")
    with transaction.atomic():
        role.permissions.set(Permission.objects.filter(code__in=chosen))
    return len(chosen)


def create_role(*, code, name, description="", is_active=False,
                permission_codes=None) -> Role:
    """创建自定义角色（内置角色只能由 ``sync_rbac`` 维护，任何入口都不允许新建）。"""
    check_role_code_available(code)
    with transaction.atomic():
        role = Role.objects.create(
            code=code,
            name=name,
            description=description or "",
            is_active=bool(is_active),
            is_builtin=False,
        )
        if permission_codes is not None:
            save_role_permissions(role, permission_codes)
    return role


def update_role(role, *, name=None, description=None, is_active=None,
                permission_codes=None) -> Role:
    """更新角色。

    两条与页面端一致的不变式：
    1. ``code`` 永不修改；
    2. 内置角色只允许改权限矩阵，名称/说明/启停保持锁定（``sync_rbac`` 默认也不重置）。
    """
    with transaction.atomic():
        if permission_codes is not None:
            save_role_permissions(role, permission_codes)
        if not role.is_builtin:
            if name is not None:
                role.name = name
            if description is not None:
                role.description = description
            if is_active is not None:
                role.is_active = is_active
        role.save()
    return role


def delete_role(role) -> int:
    """删除自定义角色，返回原关联用户数。内置角色拒绝删除。"""
    if role.is_builtin:
        raise AccountRuleError("内置角色不允许删除。")
    user_count = role.users.count()
    role.delete()
    return user_count
