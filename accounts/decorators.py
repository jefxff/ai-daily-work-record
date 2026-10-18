"""函数视图权限装饰器。

- @perm_required(module, action)：单模块动作权限（全站业务视图使用）
- @require_any_module：只要求"至少有一个模块的可见权限"（首页/全局搜索使用）

匿名用户：页面 302 到登录页（带 next），AJAX/API 返回 401 JSON；
已登录无权限：页面渲染 403（HTTP 403），AJAX/API 返回 403 JSON；
有权限：正常执行视图。
"""

import functools
from urllib.parse import quote

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import redirect, render

from .services import has_perm, visible_modules


def _is_json_request(request):
    path = request.path or ""
    return (
        path.startswith("/api/")
        or request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("accept") or "")
    )


def perm_required(module, action):
    def decorator(view_func):
        @functools.wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            user = getattr(request, "user", None)
            if not getattr(user, "is_authenticated", False):
                if _is_json_request(request):
                    return JsonResponse(
                        {"detail": "身份验证未通过，请先登录。"}, status=401
                    )
                next_url = quote(request.get_full_path(), safe="/?&=")
                return redirect(f"{settings.LOGIN_URL}?next={next_url}")
            if not has_perm(user, module, action):
                if _is_json_request(request):
                    return JsonResponse(
                        {"detail": "权限不足，无法执行该操作。"}, status=403
                    )
                return render(
                    request,
                    "accounts/403.html",
                    {"required_module": module, "required_action": action},
                    status=403,
                )
            return view_func(request, *args, **kwargs)

        # 供静态检查/中间件识别（全站权限覆盖审计用）
        _wrapped.rbac_permission = (module, action)
        return _wrapped

    return decorator


def require_any_module(view_func):
    """要求用户至少拥有一个模块的任一权限。

    用于首页之外的"跨模块页面"（如全站搜索）：这类页面没有单一归属模块，
    用 @perm_required 会误伤部分角色；但完全不做判定，零权限账号又能通过它
    触达全站数据（审计 #6）。
    """

    @functools.wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        user = getattr(request, "user", None)
        if not getattr(user, "is_authenticated", False):
            if _is_json_request(request):
                return JsonResponse({"detail": "身份验证未通过，请先登录。"}, status=401)
            next_url = quote(request.get_full_path(), safe="/?&=")
            return redirect(f"{settings.LOGIN_URL}?next={next_url}")
        if not visible_modules(user):
            if _is_json_request(request):
                return JsonResponse({"detail": "权限不足，无法执行该操作。"}, status=403)
            return render(
                request,
                "accounts/403.html",
                {"required_module": "任意业务模块", "required_action": "read"},
                status=403,
            )
        return view_func(request, *args, **kwargs)

    # 标记"跨模块"语义，供权限覆盖审计识别
    _wrapped.rbac_any_module = True
    return _wrapped
