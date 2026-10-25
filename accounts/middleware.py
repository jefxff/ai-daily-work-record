"""全站登录拦截中间件。

白名单（可匿名访问）：登录/登出/注册、Django admin、静态资源、媒体文件、
以及被 @login_exempt 显式标记的视图（如 /api-token-auth/）。
其余一切路径：未登录时页面 302 到登录页（带 next），/api/ 返回 401 JSON；
但 /api/ 下携带 Authorization 头（长期 Token）的请求会放行，由 DRF 处理认证。
"""

import time
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth import logout as auth_logout
from django.http import HttpResponse, JsonResponse, HttpResponseRedirect
from django.urls import resolve

from work_record.error_handler import error_payload


def _in_maintenance() -> bool:
    """数据恢复期的维护标志（sysbackup 置位；缓存不可用时按「非维护」处理）。"""
    try:
        from sysbackup.services import in_maintenance_mode

        return in_maintenance_mode()
    except Exception:  # 中间件绝不能因为标志读取失败而阻断请求
        return False


class SessionAbsoluteTimeoutMiddleware:
    """会话绝对有效期：无论是否活跃，超过上限一律强制重新登录。

    Django 原生只有滑动过期（SESSION_COOKIE_AGE + SESSION_SAVE_EVERY_REQUEST），
    会话被持续使用时可无限续期。这里在首次认证请求时记录起点，超过
    SESSION_ABSOLUTE_MAX_AGE（默认 12h）即登出（审计 P3-3）。
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.max_age = int(getattr(settings, "SESSION_ABSOLUTE_MAX_AGE", 12 * 3600))

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            now = int(time.time())
            started = request.session.get("_abs_start")
            if not started:
                request.session["_abs_start"] = now
            elif now - int(started) > self.max_age:
                auth_logout(request)
                if (request.path or "").startswith("/api/") or \
                        request.headers.get("x-requested-with") == "XMLHttpRequest":
                    return JsonResponse(
                        {"detail": "会话已超过最长有效期，请重新登录。"}, status=401
                    )
                next_url = quote(request.get_full_path(), safe="/?&=")
                return HttpResponseRedirect(f"{settings.LOGIN_URL}?next={next_url}")
        return self.get_response(request)


class LoginRequiredMiddleware:
    # 精确路径前缀，全部以 / 结尾防越权（如 /accounts/loginxxx 不匹配）
    # 注意：/media/ 不在白名单 —— 用户上传文件（证照扫描件/证明材料）必须登录后访问，
    # 由 work_record.urls 的 protected_media 视图提供（URL 路径保持 /media/... 不变）
    # 注意：/admin/ 也不在白名单（审计 SEC-09）—— Django Admin 自带登录页不受
    # 项目登录锁定保护，匿名可达即可无限爆破；现在必须先在项目登录页登录，
    # 且 AdminSite.has_permission 已收紧为仅超管。
    EXEMPT_PREFIXES = (
        "/accounts/login/",
        "/accounts/logout/",
        "/accounts/register/",
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
                    return JsonResponse(error_payload("登录状态已失效，请重新登录。", 401), status=401)
                return HttpResponseRedirect(settings.LOGIN_URL)
            # 数据恢复期间（审查 S2/B2）：拒绝非超管的写请求，避免半状态继续产生
            # 新数据。只在写方法上查一次缓存，不给读请求增加开销。
            if request.method not in ("GET", "HEAD", "OPTIONS") and not user.is_superuser:
                if _in_maintenance():
                    message = "系统正在执行数据恢复，请稍后再试（恢复完成后自动解除）。"
                    if request.path.startswith("/api/") or \
                            request.headers.get("x-requested-with") == "XMLHttpRequest":
                        return JsonResponse(error_payload(message, 503), status=503)
                    return HttpResponse(message, status=503, content_type="text/plain; charset=utf-8")
            return self.get_response(request)

        if self._is_exempt(request):
            return self.get_response(request)

        if request.path.startswith("/api/"):
            # 携带 Authorization 头（如长期 Token Authorization: Token <key>）的
            # 请求在中间件层看不到会话用户，必须放行交由 DRF 认证/鉴权处理；
            # 无该头时维持原有 401 行为（浏览器 AJAX 需先登录）。
            if request.headers.get("Authorization"):
                return self.get_response(request)
            return JsonResponse(error_payload("身份验证未通过，请先登录。", 401), status=401)

        next_url = quote(request.get_full_path(), safe="/?&=")
        return HttpResponseRedirect(f"{settings.LOGIN_URL}?next={next_url}")
