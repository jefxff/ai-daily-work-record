"""系统管理视图：用户管理、角色管理、登录日志。

权限要求：均为 system 模块对应动作（perm_required），不依赖 is_staff。
"""

from django.contrib import messages
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_http_methods

from records.crud import _per_page

from .decorators import perm_required
from .forms import AdminCreateUserForm, AdminEditUserForm, AdminResetPasswordForm, RoleForm
from .models import LoginLog, Permission, Role, UserProfile
from .registry import ACTION_LABELS, MODULES, all_permission_codes, permission_code


def _forbidden(request):
    return render(request, "accounts/403.html", status=403)


def _is_ajax(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _modal_json(template, context, request, *, ok=True):
    """渲染弹窗局部表单：ok=True(200) 初始加载，ok=False(422) 校验失败。"""
    html = render_to_string(template, context, request=request)
    return JsonResponse({"ok": ok, "html": html}, status=200 if ok else 422)


def _modal_message(message):
    return JsonResponse({"ok": False, "message": str(message)}, status=422)


def _guarded_target(request, user_id):
    """取出目标用户；非超管操作超管账号一律拒绝。返回 (user, None) 或 (None, 403响应)。"""
    target = get_object_or_404(User, pk=user_id)
    if target.is_superuser and not request.user.is_superuser:
        return None, _forbidden(request)
    return target, None


@perm_required("system", "read")
def login_log_list_view(request):
    logs = LoginLog.objects.select_related("user").all()
    username = request.GET.get("username", "").strip()
    result = request.GET.get("result", "")
    if username:
        logs = logs.filter(username__icontains=username)
    if result == "success":
        logs = logs.filter(success=True)
    elif result == "failed":
        logs = logs.filter(success=False)

    # 统一分页口径：每页 10/30/50，复用共享 _pagination.html 组件
    paginator = Paginator(logs, _per_page(request))
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "accounts/login_logs.html", {
        "page": page,
        "f_username": username,
        "f_result": result,
        "total": paginator.count,
    })


# ============================== 用户管理 ==============================

@perm_required("system", "read")
def user_list_view(request):
    users = User.objects.select_related("profile").prefetch_related("rbac_roles", "groups")
    keyword = request.GET.get("q", "").strip()
    role_id = request.GET.get("role", "")
    status = request.GET.get("status", "")
    if keyword:
        users = users.filter(username__icontains=keyword) | users.filter(
            profile__real_name__icontains=keyword
        )
        users = users.distinct()
    if role_id.isdigit():
        users = users.filter(rbac_roles__pk=role_id)
    if status == "active":
        users = users.filter(is_active=True)
    elif status == "inactive":
        users = users.filter(is_active=False)

    users = users.order_by("-is_superuser", "-id")
    # 统一分页口径：每页 10/30/50，复用共享 _pagination.html 组件
    paginator = Paginator(users, _per_page(request))
    page = paginator.get_page(request.GET.get("page"))
    roles = Role.objects.filter(is_active=True)
    return render(request, "accounts/user_list.html", {
        "page": page,
        "roles": roles,
        "f_q": keyword,
        "f_role": role_id,
        "f_status": status,
        "total": paginator.count,
    })


def _parse_role_ids(request):
    valid_ids = set(Role.objects.filter(is_active=True).values_list("id", flat=True))
    selected = set()
    for raw in request.POST.getlist("roles"):
        if raw.isdigit() and int(raw) in valid_ids:
            selected.add(int(raw))
    return selected


@perm_required("system", "create")
@require_http_methods(["GET", "POST"])
def user_create_view(request):
    roles = Role.objects.filter(is_active=True)
    is_ajax = _is_ajax(request)
    if request.method == "POST":
        form = AdminCreateUserForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            user = User.objects.create_user(
                username=data["username"], password=data["password"],
                is_active=data.get("is_active", True),
            )
            UserProfile.objects.create(
                user=user,
                real_name=data.get("real_name", ""),
                department=data.get("department", ""),
                position=data.get("position", ""),
            )
            user.rbac_roles.set(_parse_role_ids(request))
            messages.success(request, f"用户「{user.username}」创建成功。")
            if is_ajax:
                return JsonResponse({"ok": True, "message": f"用户「{user.username}」创建成功"})
            return redirect("accounts:user_edit", user_id=user.pk)
        if is_ajax:
            return _modal_json("accounts/_user_form_modal.html", {
                "form": form, "roles": roles, "selected_role_ids": _parse_role_ids(request),
                "mode": "create",
            }, request, ok=False)
    else:
        form = AdminCreateUserForm()
    if is_ajax:
        return _modal_json("accounts/_user_form_modal.html", {
            "form": form, "roles": roles, "selected_role_ids": set(), "mode": "create",
        }, request)
    return render(request, "accounts/user_form.html", {
        "form": form, "roles": roles, "selected_role_ids": set(), "mode": "create",
    })


@perm_required("system", "update")
@require_http_methods(["GET", "POST"])
def user_edit_view(request, user_id):
    target, denied = _guarded_target(request, user_id)
    if denied is not None:
        return denied
    roles = Role.objects.filter(is_active=True)
    profile = getattr(target, "profile", None)
    is_ajax = _is_ajax(request)
    if request.method == "POST":
        form = AdminEditUserForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            if profile is None:
                profile = UserProfile.objects.create(user=target)
            profile.real_name = data.get("real_name", "")
            profile.department = data.get("department", "")
            profile.position = data.get("position", "")
            profile.save()
            target.is_active = data.get("is_active", False)
            # is_staff（Admin 后台入口）仅超级管理员可授予
            if request.user.is_superuser:
                target.is_staff = data.get("is_staff", False)
            target.save()
            target.rbac_roles.set(_parse_role_ids(request))
            messages.success(request, f"用户「{target.username}」资料已更新。")
            if is_ajax:
                return JsonResponse({"ok": True, "message": f"用户「{target.username}」资料已更新"})
            return redirect("accounts:user_edit", user_id=target.pk)
        if is_ajax:
            return _modal_json("accounts/_user_form_modal.html", {
                "form": form, "roles": roles, "selected_role_ids": _parse_role_ids(request),
                "target": target, "mode": "edit",
            }, request, ok=False)
    else:
        form = AdminEditUserForm(initial={
            "real_name": profile.real_name if profile else "",
            "department": profile.department if profile else "",
            "position": profile.position if profile else "",
            "is_active": target.is_active,
            "is_staff": target.is_staff,
        })
    if is_ajax:
        return _modal_json("accounts/_user_form_modal.html", {
            "form": form, "roles": roles,
            "selected_role_ids": set(target.rbac_roles.values_list("id", flat=True)),
            "target": target, "mode": "edit",
        }, request)
    return render(request, "accounts/user_form.html", {
        "form": form, "roles": roles,
        "selected_role_ids": set(target.rbac_roles.values_list("id", flat=True)),
        "target": target, "mode": "edit",
    })


def _set_active(request, user_id, active, *, self_check=True):
    target, denied = _guarded_target(request, user_id)
    if denied is not None:
        return denied
    if self_check and not active and target.pk == request.user.pk:
        messages.error(request, "不能停用当前登录账号。")
        return redirect("accounts:user_list")
    target.is_active = active
    target.save()
    return target


@perm_required("system", "update")
@require_http_methods(["POST"])
def user_toggle_view(request, user_id):
    result = _set_active(
        request, user_id, request.POST.get("active") == "true")
    if not isinstance(result, User):
        return result
    messages.success(request, f"用户「{result.username}」已{'启用' if result.is_active else '停用'}。")
    return redirect("accounts:user_list")


@perm_required("system", "update")
@require_http_methods(["POST"])
def user_approve_view(request, user_id):
    result = _set_active(request, user_id, True, self_check=False)
    if not isinstance(result, User):
        return result
    messages.success(request, f"已审核通过「{result.username}」，该账号现在可以登录。")
    return redirect("accounts:user_list")


@perm_required("system", "update")
@require_http_methods(["POST"])
def user_reject_view(request, user_id):
    result = _set_active(request, user_id, False, self_check=False)
    if not isinstance(result, User):
        return result
    messages.success(request, f"已拒绝/停用「{result.username}」。")
    return redirect("accounts:user_list")


@perm_required("system", "update")
@require_http_methods(["GET", "POST"])
def user_reset_password_view(request, user_id):
    target, denied = _guarded_target(request, user_id)
    if denied is not None:
        return denied
    if request.method == "POST":
        form = AdminResetPasswordForm(request.POST)
        if form.is_valid():
            target.set_password(form.cleaned_data["new_password"])
            target.save()
            # 密码指纹机制会使该用户其它会话在下次请求时被强制登出
            messages.success(request, f"「{target.username}」的密码已重置。")
            return redirect("accounts:user_list")
    else:
        form = AdminResetPasswordForm()
    return render(request, "accounts/user_reset_password.html", {"form": form, "target": target})


# ============================== 角色管理 ==============================

def _matrix(selected_codes):
    """构造权限矩阵渲染数据：固定 C/R/U/D 四列，模块不支持的动作为 None。"""
    columns = ["create", "read", "update", "delete"]
    matrix = []
    for module in MODULES:
        supported = set(module["actions"])
        actions = []
        for col in columns:
            if col in supported:
                code = permission_code(module["key"], col)
                actions.append({"label": ACTION_LABELS[col], "code": code,
                                "checked": code in selected_codes})
            else:
                actions.append(None)
        matrix.append({
            "key": module["key"],
            "name": module["name"],
            "actions": actions,
            "is_system": module["key"] == "system",
        })
    return matrix


@perm_required("system", "read")
def role_list_view(request):
    roles = Role.objects.prefetch_related("users").all().order_by("-is_builtin", "code")
    return render(request, "accounts/role_list.html", {"roles": roles})


def _save_role_permissions(role, raw_codes):
    """校验并整体替换角色权限；非法 code 返回错误信息字符串，成功返回 None。"""
    valid_codes = set(all_permission_codes())
    chosen = {c for c in raw_codes if c}
    illegal = chosen - valid_codes
    if illegal:
        return f"检测到非法权限码，已拒绝保存：{', '.join(sorted(illegal))}"
    with transaction.atomic():
        perms = Permission.objects.filter(code__in=chosen)
        role.permissions.set(perms)
    return None


@perm_required("system", "create")
@require_http_methods(["GET", "POST"])
def role_create_view(request):
    is_ajax = _is_ajax(request)
    illegal_err = ""
    if request.method == "POST":
        form = RoleForm(request.POST)
        code = (request.POST.get("code") or "").strip()
        if form.is_valid() and Role.objects.filter(code__iexact=code).exists():
            form.add_error("code", "角色编码已存在。")
        if form.is_valid():
            with transaction.atomic():
                role = Role.objects.create(
                    code=form.cleaned_data["code"],
                    name=form.cleaned_data["name"],
                    description=form.cleaned_data.get("description", ""),
                    is_active=form.cleaned_data.get("is_active", False),
                    is_builtin=False,
                )
                err = _save_role_permissions(role, request.POST.getlist("perms"))
                if err:
                    transaction.set_rollback(True)
                    illegal_err = err
                    messages.error(request, err)
                else:
                    messages.success(request, f"角色「{role.name}」创建成功。")
                    if is_ajax:
                        return JsonResponse({"ok": True, "message": f"角色「{role.name}」创建成功"})
                    return redirect("accounts:role_list")
        if is_ajax:
            if illegal_err:
                return _modal_message(illegal_err)
            return _modal_json("accounts/_role_form_modal.html", {
                "form": form, "matrix": _matrix(set(request.POST.getlist("perms"))), "mode": "create",
            }, request, ok=False)
    else:
        form = RoleForm()
    if is_ajax:
        return _modal_json("accounts/_role_form_modal.html", {
            "form": form, "matrix": _matrix(set()), "mode": "create",
        }, request)
    return render(request, "accounts/role_form.html", {
        "form": form, "matrix": _matrix(set()), "mode": "create",
    })


@perm_required("system", "update")
@require_http_methods(["GET", "POST"])
def role_edit_view(request, role_id):
    role = get_object_or_404(Role, pk=role_id)
    is_ajax = _is_ajax(request)
    illegal_err = ""
    if request.method == "POST":
        form = RoleForm(request.POST)
        if form.is_valid():
            # code 永不修改（内置/自定义均如此）；内置角色仅开放权限矩阵，
            # 名称/说明/启停保持锁定；sync_rbac 默认不再重置内置角色权限
            err = _save_role_permissions(role, request.POST.getlist("perms"))
            if err:
                illegal_err = err
                messages.error(request, err)
            else:
                if not role.is_builtin:
                    role.name = form.cleaned_data["name"]
                    role.description = form.cleaned_data.get("description", "")
                    role.is_active = form.cleaned_data.get("is_active", False)
                role.save()
                messages.success(request, f"角色「{role.name}」权限已更新。")
                if is_ajax:
                    return JsonResponse({"ok": True, "message": f"角色「{role.name}」权限已更新"})
                return redirect("accounts:role_list")
        if is_ajax:
            if illegal_err:
                return _modal_message(illegal_err)
            return _modal_json("accounts/_role_form_modal.html", {
                "form": form,
                "matrix": _matrix(set(request.POST.getlist("perms"))),
                "role": role, "mode": "edit",
            }, request, ok=False)
    else:
        form = RoleForm(initial={
            "code": role.code, "name": role.name,
            "description": role.description, "is_active": role.is_active,
        })
    if is_ajax:
        return _modal_json("accounts/_role_form_modal.html", {
            "form": form, "matrix": _matrix(set(role.permissions.values_list("code", flat=True))),
            "role": role, "mode": "edit",
        }, request)
    return render(request, "accounts/role_form.html", {
        "form": form, "matrix": _matrix(set(role.permissions.values_list("code", flat=True))),
        "role": role, "mode": "edit",
    })


@perm_required("system", "delete")
@require_http_methods(["POST"])
def role_delete_view(request, role_id):
    role = get_object_or_404(Role, pk=role_id)
    if role.is_builtin:
        return _forbidden(request)
    user_count = role.users.count()
    role_name = role.name
    role.delete()
    messages.success(request, f"角色「{role_name}」已删除，已解除与 {user_count} 个用户的关联。")
    return redirect("accounts:role_list")

