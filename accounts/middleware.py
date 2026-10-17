"""全站登录拦截中间件。

白名单（可匿名访问）：登录/登出/注册、Django admin、静态资源、媒体文件。
其余一切路径：未登录时页面 302 到登录页（带 next），/api/ 返回 401 JSON。
"""

from urllib.parse import quote

from django.conf import settings
from django.contrib.auth import logout as auth_logout
from django.http import JsonResponse, HttpResponseRedirect
from django.urls import resolve


class LoginRequiredMiddleware:
    # 精确路径前缀，全部以 / 结尾防越权（如 /accounts/loginxxx 不匹配）
    # 注意：/media/ 不在白名单 —— 用户上传文件（证照扫描件/证明材料）必须登录后访问，
    # 由 work_record.urls 的 protected_media 视图提供（URL 路径保持 /media/... 不变）
    EXEMPT_PREFIXES = (
        "/accounts/login/",
        "/accounts/logout/",
        "/accounts/register/",
        "/admin/",
        "/static/",
        "/favicon.ico",
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def _is_exempt(self, request):
        path = request.path or "/"
        if path in ("/favicon.ico",):
            return True
        for prefix in self.EXEMPT_PREFIXES:
            if path == prefix.rstrip("/") or path.startswith(prefix):
                return True
        # 视图显式标记免登录（@login_exempt）
        try:
            match = resolve(path)
        except Exception:
            return False
        view_func = getattr(match, "func", None)
        return bool(getattr(view_func, "rbac_public", False))

    def __call__(self, request):
        user = getattr(request, "user", None)
        if getattr(user, "is_authenticated", False):
            # 密码变更/管理员重置后，其它会话的密码指纹失效 → 强制登出
            stored_pwd = request.session.get("_pwd")
            if stored_pwd and stored_pwd != user.password:
                auth_logout(request)
                if request.path.startswith("/api/"):
                    return JsonResponse({"detail": "登录状态已失效，请重新登录。"}, status=401)
                return HttpResponseRedirect(settings.LOGIN_URL)
            return self.get_response(request)

        if self._is_exempt(request):
            return self.get_response(request)

        if request.path.startswith("/api/"):
            return JsonResponse({"detail": "身份验证未通过，请先登录。"}, status=401)

        next_url = quote(request.get_full_path(), safe="/?&=")
        return HttpResponseRedirect(f"{settings.LOGIN_URL}?next={next_url}")
