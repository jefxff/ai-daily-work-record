"""archive 模块共享辅助：操作人展示名、操作留痕、AJAX 判定。

供 views.py 与各域视图子模块（views_license 等）共用，避免循环导入。
函数实现与拆分前逐字节一致，仅模块位置变化。
"""

from records.audit import log_operation, operator_from_request
from work_record.http_utils import is_ajax as _is_ajax  # 统一实现（复核报告 CODE-01）


def _operator(request) -> str:
    """获取操作人展示名（姓名优先）。

    实现已收口到 ``records.audit.operator_from_request``，与留痕写入共用同一套
    推导规则；这里保留函数名以兼容各视图子模块的既有引用。
    """
    return operator_from_request(request, prefer_real_name=True)


def _log(request, op_type: str, obj_id: str, summary: str):
    """写入操作留痕（复用 records.audit 的统一写入点）。

    ``prefer_real_name=True`` 保持档案模块的历史口径：操作人优先展示 Profile 姓名，
    没有姓名才退回用户名。
    """
    log_operation(
        request, "档案管理", op_type, obj_id, summary, prefer_real_name=True,
    )


