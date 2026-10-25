"""登录后自助功能：修改密码、我的会话管理。"""

from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.sessions.models import Session
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from records.audit import log_operation


@require_http_methods(["GET", "POST"])
def password_change_view(request):
    """登录后自助修改密码。成功后仅保留当前会话，其它会话由中间件指纹校验强制失效。"""
    if request.method == "POST":
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            form.save()
            log_operation(
                request, "系统管理", '修改密码', f'用户#{request.user.id}（{request.user.username}）',
                '自助修改登录密码（其它会话将被强制登出）',
            )
            # 保持当前会话登录态，并把密码指纹刷新为新值
            update_session_auth_hash(request, form.user)
            request.session["_pwd"] = form.user.password
            return redirect("accounts:password_done")
    else:
        form = PasswordChangeForm(request.user)
    return render(request, "accounts/password.html", {"form": form})


def password_done_view(request):
    return render(request, "accounts/password.html", {"done": True})


def _parse_dt(value):
    try:
        return timezone.datetime.fromisoformat(value) if value else None
    except (ValueError, TypeError):
        return None


def _user_sessions(user):
    """读取某用户当前全部有效会话，标记是否为当前会话。"""
    items = []
    for s in Session.objects.filter(expire_date__gt=timezone.now()):
        data = s.get_decoded()
        uid = data.get("_auth_user_id")
        if uid is None or int(uid) != user.pk:
            continue
        items.append({
            "key": s.session_key,
            "login_ts": _parse_dt(data.get("login_ts")),
            "login_ip": data.get("login_ip", ""),
            "login_ua": data.get("login_ua", ""),
            "expire_at": s.expire_date,
        })
    return items


@require_http_methods(["GET"])
def session_list_view(request):
    sessions = _user_sessions(request.user)
    current_key = request.session.session_key
    for item in sessions:
        item["is_current"] = item["key"] == current_key
    sessions.sort(key=lambda x: (not x["is_current"], x["login_ts"] or timezone.now()))
    return render(request, "accounts/sessions.html", {"sessions": sessions})


@require_http_methods(["POST"])
def session_revoke_view(request, session_key):
    """下线指定会话（不允许下线当前会话；当前会话请走登出）。"""
    if session_key and session_key != request.session.session_key:
        Session.objects.filter(session_key=session_key).delete()
        log_operation(
            request, "系统管理", '撤销会话', f'用户#{request.user.id}（{request.user.username}）',
            f'下线指定会话: …{session_key[-8:]}',
        )
    return redirect("accounts:sessions")


@require_http_methods(["POST"])
def session_revoke_others_view(request):
    """下线除当前会话外的全部会话。"""
    current_key = request.session.session_key
    revoked = 0
    for s in Session.objects.filter(expire_date__gt=timezone.now()):
        if s.session_key == current_key:
            continue
        data = s.get_decoded()
        if data.get("_auth_user_id") and int(data["_auth_user_id"]) == request.user.pk:
            s.delete()
            revoked += 1
    if revoked:
        log_operation(
            request, "系统管理", '撤销会话', f'用户#{request.user.id}（{request.user.username}）',
            f'下线除当前会话外的全部会话（共 {revoked} 个）',
        )
    return redirect("accounts:sessions")
