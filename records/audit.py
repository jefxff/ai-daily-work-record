"""操作留痕写入与「变更前/后」快照。

``snapshot(obj)`` 把模型实例归一为 JSON 安全的字典，供 ``log_operation`` 的
``before`` / ``after`` 参数使用，最终落库到 ``OperationLog.before_json/after_json``。
"""
import uuid
from datetime import date, datetime, time
from decimal import Decimal

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


def write_operation(operator, module_type, operation_type, object_identifier,
                    change_summary='', before=None, after=None):
    """全站唯一的操作留痕写入点。

    各模块的 ``_log`` 封装（samples / archive 等）都应走这里，而不是自己
    ``OperationLog.objects.create(...)`` —— 否则字段口径（空值处理、快照序列化）
    会各写一份，日后加字段就得改 N 处。
    """
    OperationLog.objects.create(
        operator=operator or '未知',
        module_type=module_type,
        operation_type=operation_type,
        object_identifier=object_identifier,
        change_summary=change_summary or '',
        before_json=before,
        after_json=after,
    )


def log_operation(request, module_type, operation_type, object_identifier,
                  change_summary='', before=None, after=None, *,
                  prefer_real_name=False):
    """记录操作留痕：优先记录登录用户名，未登录时 fallback 到 IP 地址。

    before / after 为 ``snapshot()`` 归一后的字典（可为 None）：
    新增仅传 after、删除仅传 before、编辑两者都传；导出等无实体变更的动作不传。
    """
    write_operation(
        operator_from_request(request, prefer_real_name=prefer_real_name),
        module_type, operation_type, object_identifier,
        change_summary, before, after,
    )
