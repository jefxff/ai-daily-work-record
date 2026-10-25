"""操作留痕写入与「变更前/后」快照。

``snapshot(obj)`` 把模型实例归一为 JSON 安全的字典，供 ``log_operation`` 的
``before`` / ``after`` 参数使用，最终落库到 ``OperationLog.before_json/after_json``。
"""
import uuid
from datetime import date, datetime, time
from decimal import Decimal

from .audit_vocab import normalize_action, normalize_module
from .models import OperationLog

# 单个字段值保留的最大字符数：知识库正文等大字段截断，避免留痕表被撑爆
MAX_SNAPSHOT_CHARS = 2000


def _jsonable(value):
    """把任意字段值归一为 JSON 可存储的形式（超长字符串截断）。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > MAX_SNAPSHOT_CHARS:
            return value[:MAX_SNAPSHOT_CHARS] + '…'
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (Decimal, uuid.UUID)):
        return str(value)
    return str(value)


def snapshot(obj):
    """把一个模型实例拍成 {字段名: 值} 的 JSON 安全字典。

    仅取具体字段（``concrete_fields``，外键记录为 ``xxx_id``），不含多对多；
    值经 ``_jsonable`` 归一。``obj`` 为 None 时返回 None（新增无 before）。
    """
    if obj is None:
        return None
    data = {}
    for field in obj._meta.concrete_fields:
        try:
            data[field.name] = _jsonable(field.value_from_object(obj))
        except Exception:
            continue
    return data


def operator_from_request(request, *, prefer_real_name=False):
    """从 request 推导操作人显示名。

    ``prefer_real_name=True`` 时优先用 ``Profile.姓名``（档案模块的历史口径），
    默认用用户名（系统/业务模块口径）。两者指向同一个人，只是展示习惯不同 ——
    协议统一到这一处，避免各模块自己拼一套（查日志时口径不一致很难对账）。
    未登录时 fallback 到 X-Forwarded-For 首个地址，再 fallback 到 REMOTE_ADDR。
    """
    if not request:
        return '未知'
    user = getattr(request, 'user', None)
    if user is not None and getattr(user, 'is_authenticated', False):
        if prefer_real_name:
            profile = getattr(user, 'profile', None)
            if profile and getattr(profile, 'real_name', ''):
                return profile.real_name
        return user.get_username()
    # 未登录/无 request.user 时 fallback 到 IP（RBAC 全站登录后理论不会走到）
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '未知')


def _resolve_username(display_name):
    """展示名 → 登录账号（尽力而为，无法唯一确定时返回空串）。

    为什么需要：``OperationLog.operator`` 存的是**展示名**，各模块口径不同 ——
    多数记用户名，档案/证照/试样记 Profile 姓名。于是同一个人的操作在审计表里
    变成「张三」和「zhangsan」两条，按操作人聚合的异常检测（单小时高频、导出
    爆发）会把一个人拆成两个人，阈值永远打不到（一致性审计 P0-2）。

    写入时顺手把账号补齐，聚合统一按 ``operator_username``，展示仍用 ``operator``。
    姓名重名时不猜（返回空串），聚合回退到展示名 —— 宁可少合并，不要错合并。
    """
    display_name = (display_name or "").strip()
    if not display_name:
        return ""
    from django.contrib.auth.models import User

    if User.objects.filter(username=display_name).exists():
        return display_name
    from accounts.models import UserProfile

    candidates = list(
        UserProfile.objects.filter(real_name=display_name)
        .values_list("user__username", flat=True)[:2]
    )
    return candidates[0] if len(candidates) == 1 else ""


def operator_identity(request, *, prefer_real_name=False):
    """返回 ``(展示名, 登录账号)``：账号用于聚合，展示名用于显示。"""
    display = operator_from_request(request, prefer_real_name=prefer_real_name)
    user = getattr(request, 'user', None) if request else None
    if user is not None and getattr(user, 'is_authenticated', False):
        return display, user.get_username()
    return display, ""


def write_operation(operator, module_type, operation_type, object_identifier,
                    change_summary='', before=None, after=None,
                    operator_username=""):
    """全站唯一的操作留痕写入点。

    各模块的 ``_log`` 封装（samples / archive / sysbackup 等）都应走这里，而不是
    自己 ``OperationLog.objects.create(...)`` —— 否则字段口径（空值处理、快照序列化、
    词表归一）会各写一份，日后加字段就得改 N 处。

    模块名与动作名在此统一折叠成标准词（``records.audit_vocab``）：
    历史行不动，新行从此一致；查询与审计规则用 ``expand_*`` 兼容旧词。
    """
    operator = (operator or '未知').strip() or '未知'
    if not operator_username:
        operator_username = _resolve_username(operator)
    OperationLog.objects.create(
        operator=operator,
        operator_username=operator_username,
        module_type=normalize_module(module_type),
        operation_type=normalize_action(operation_type),
        object_identifier=object_identifier,
        change_summary=change_summary or '',
        before_json=before,
        after_json=after,
    )


def log_operation(request, module_type, operation_type, object_identifier,
                  change_summary='', before=None, after=None, *,
                  prefer_real_name=False):
    """记录操作留痕：展示名按口径取姓名/用户名，账号单独落库（供聚合）。

    before / after 为 ``snapshot()`` 归一后的字典（可为 None）：
    新增仅传 after、删除仅传 before、编辑两者都传；导出等无实体变更的动作不传。
    """
    display, username = operator_identity(request, prefer_real_name=prefer_real_name)
    write_operation(
        display, module_type, operation_type, object_identifier,
        change_summary, before, after, operator_username=username,
    )
