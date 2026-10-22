"""认证视图：登录、登出、注册。

用户/角色/会话/日志等管理视图位于 views_admin.py。
"""

import time
from urllib.parse import urlparse

from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.contrib.auth.models import User
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from .forms import LoginForm, RegisterForm
from .models import LoginLog, UserProfile
from .security import (
    clear_failures, client_ip, get_failure_count, is_locked,
    record_failure, record_register, register_throttled,
)


def _log(request, *, user, username, success, reason="", login_type=LoginLog.LoginType.LOGIN):
    LoginLog.objects.create(
        user=user if (user and user.pk) else None,
        username=username or "",
        ip=client_ip(request),
        user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:400],
        login_type=login_type,
        success=success,
        reason=reason,
    )


def _safe_next(next_url):
    """仅允许同源相对路径，防开放重定向。"""
    if not next_url:
        return "/"
    parsed = urlparse(next_url)
    if parsed.scheme or parsed.netloc:
        return "/"
    if not next_url.startswith("/") or next_url.startswith("//"):
        return "/"
    return next_url


@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        return redirect("/")
    form = LoginForm()
    error_msg = ""
    if request.method == "POST":
        form = LoginForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data["username"].strip()
            password = form.cleaned_data["password"]
            ip = client_ip(request)

            locked, _left = is_locked(ip, username)
            if locked:
                error_msg = "登录失败次数过多，账号已锁定 15 分钟，请稍后再试或联系管理员。"
                _log(request, user=None, username=username, success=False,
                     reason="账号锁定中", login_type=LoginLog.LoginType.LOGIN)
            else:
                user = authenticate(request, username=username, password=password)
                if user is not None:
                    clear_failures(ip, username)
                    auth_login(request, user)
                    # 会话指纹：用于改密/管理员重置后令其它会话失效；记录登录元信息
                    request.session["_pwd"] = user.password
                    from django.utils.timezone import now
                    request.session["login_ts"] = now().isoformat()
                    request.session["login_ip"] = ip
                    request.session["login_ua"] = (request.META.get("HTTP_USER_AGENT") or "")[:300]
                    _log(request, user=user, username=username, success=True)
                    return redirect(_safe_next(request.GET.get("next") or request.POST.get("next")))

                # 统一错误文案——不暴露账号是否存在、是否待审核/已停用（防用户名枚举）
                error_msg = "用户名或密码不正确。"
                reason = "用户名或密码错误"

                # 递增延迟：第 3 次失败起，每次 sleep (count - 2) * 2 秒，纯服务端零前端成本
                count = get_failure_count(ip, username)
                if count >= 3:
                    delay = (count - 2) * 2
                    time.sleep(delay)

                record_failure(ip, username)
                _log(request, user=None, username=username, success=False, reason=reason)

    return render(
        request,
        "accounts/login.html",
        {"form": form, "error_msg": error_msg, "next_url": request.GET.get("next", "/")},
    )


@require_http_methods(["POST"])
def logout_view(request):
    user = request.user if request.user.is_authenticated else None
    if user is not None:
        _log(request, user=user, username=user.username, success=True,
             reason="用户登出", login_type=LoginLog.LoginType.LOGOUT)
    auth_logout(request)
    return redirect("/accounts/login/")


@require_http_methods(["GET", "POST"])
def register_view(request):
    if request.user.is_authenticated:
        return redirect("/")
    submitted = False
    ip = client_ip(request)
    if request.method == "POST":
        # L7：注册限流——同一 IP 每小时最多 5 次提交，防刷号
        throttled, _used = register_throttled(ip)
        if throttled:
            form = RegisterForm()
            return render(request, "accounts/register.html", {
                "form": form,
                "submitted": False,
                "throttle_error": "提交过于频繁，请一小时后再试。",
            }, status=429)
        record_register(ip)
        form = RegisterForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            user = User.objects.create_user(
                username=data["username"], password=data["password"], is_active=False
            )
            UserProfile.objects.create(
                user=user,
                real_name=data.get("real_name", ""),
                department=data.get("department", ""),
                position=data.get("position", ""),
            )
            _log(request, user=user, username=data["username"], success=True,
                 reason="新用户注册申请，等待管理员审核", login_type=LoginLog.LoginType.REGISTER)
            submitted = True
            form = RegisterForm()
    else:
        form = RegisterForm()
    return render(request, "accounts/register.html", {"form": form, "submitted": submitted})
