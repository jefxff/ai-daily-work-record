"""DRF 权限：与全站 RBAC 共用 accounts.registry 权限码。

ViewSet 声明类属性 ``perm_module``（registry 中的模块 key），
本类按 DRF action 映射到 C/R/U/D：
list/retrieve→read、create→create、update/partial_update→update、destroy→delete。

- 未登录：拒绝（DRF 返回 401）。
- 超级管理员：全部放行。
- 未声明 perm_module 的视图：仅要求登录（如 API 根索引）。
- 声明了 perm_module 但出现未映射自定义 action：默认拒绝。
"""

from rest_framework.permissions import BasePermission

from .services import has_perm

ACTION_MAP = {
    "list": "read",
    "retrieve": "read",
    "create": "create",
    "update": "update",
    "partial_update": "update",
    "destroy": "delete",
}


class ModuleRbacPermission(BasePermission):
    """按模块 + DRF action 校验 RBAC 权限。"""

    message = "您没有执行该操作的权限。"

    def has_permission(self, request, view):
        user = request.user
        if not getattr(user, "is_authenticated", False):
            return False
        if user.is_superuser:
            return True
        module = getattr(view, "perm_module", None)
        if not module:
            # 未声明模块的接口（如 API 根索引）：登录即可访问
            return True
        action = ACTION_MAP.get(getattr(view, "action", None))
        if action is None:
            # 未映射的自定义 action：默认拒绝，避免越权
            return False
        return has_perm(user, module, action)
