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


def log_operation(request, module_type, operation_type, object_identifier,
                  change_summary='', before=None, after=None):
    """记录操作留痕：优先记录登录用户名，未登录时 fallback 到 IP 地址。

    before / after 为 ``snapshot()`` 归一后的字典（可为 None）：
    新增仅传 after、删除仅传 before、编辑两者都传；导出等无实体变更的动作不传。
    """
    operator = '未知'
    if request:
        if hasattr(request, 'user') and request.user.is_authenticated:
            operator = request.user.get_username()
        else:
            # 未登录请求 fallback 到 IP（RBAC 全站登录后理论上不会走到这里）
            xff = request.META.get('HTTP_X_FORWARDED_FOR')
            if xff:
                operator = xff.split(',')[0].strip()
            else:
                operator = request.META.get('REMOTE_ADDR', '未知')
    OperationLog.objects.create(
        operator=operator,
        module_type=module_type,
        operation_type=operation_type,
        object_identifier=object_identifier,
        change_summary=change_summary or '',
        before_json=before,
        after_json=after,
    )
