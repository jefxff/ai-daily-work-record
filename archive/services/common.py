"""archive 模块共享辅助：操作人展示名、操作留痕、AJAX 判定。

供 views.py 与各域视图子模块（views_license 等）共用，避免循环导入。
函数实现与拆分前逐字节一致，仅模块位置变化。
"""

from records.models import OperationLog
from work_record.http_utils import is_ajax as _is_ajax  # 统一实现（复核报告 CODE-01）


def _operator(request) -> str:
    """获取操作人展示名。"""
    user = request.user
    profile = getattr(user, "profile", None)
    if profile and getattr(profile, "real_name", ""):
        return profile.real_name
    return user.username


def _log(request, op_type: str, obj_id: str, summary: str):
    """写入操作留痕（复用 records.OperationLog）。"""
    OperationLog.objects.create(
        operator=_operator(request),
        module_type="档案管理",
        operation_type=op_type,
        object_identifier=obj_id,
        change_summary=summary,
    )


