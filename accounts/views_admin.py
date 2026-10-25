"""系统管理视图：用户管理、角色管理、登录日志。

权限要求：均为 system 模块对应动作（perm_required），不依赖 is_staff。
"""

from django.contrib import messages
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.views.decorators.http import require_http_methods

from records.audit import log_operation, snapshot
from records.crud import _per_page
from work_record.http_utils import is_ajax as _is_ajax  # 统一实现（复核报告 CODE-01）

from . import user_admin as ua
from .decorators import perm_required
from .forms import AdminCreateUserForm, AdminEditUserForm, AdminResetPasswordForm, RoleForm
from .models import LoginLog, Role
from .registry import ACTION_LABELS, MODULES, permission_code

# 操作留痕统一模块名（与 ai_config 等系统页面对齐）
AUDIT_MODULE = "系统管理"


def _user_roles_summary(user) -> str:
    """用户当前挂载的角色名列表（用于留痕摘要）。"""
    names = list(user.rbac_roles.values_list("name", flat=True))
    return "、".join(names) if names else "（无角色）"


def _forbidden(request):
    return render(request, "accounts/403.html", status=403)


def _modal_json(template, context, request, *, ok=True):
    """渲染弹窗局部表单：ok=True(200) 初始加载，ok=False(422) 校验失败。"""
    html = render_to_string(template, context, request=request)
    return JsonResponse({"ok": ok, "html": html}, status=200 if ok else 422)


def _modal_message(message):
    return JsonResponse({"ok": False, "message": str(message)}, status=422)


def _guarded_target(request, user_id):
    """取出目标用户；非超管操作超管账号一律拒绝。返回 (user, None) 或 (None, 403响应)。

    「非超管不能动超管」的判定在 ``accounts.user_admin.may_manage_user``，
    与 DRF 端共用同一实现（避免两套规则漂移）。
    """
    target = get_object_or_404(User, pk=user_id)
    if not ua.may_manage_user(request.user, target):
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
            # 创建规则（初始密码必填、档案建空、角色挂载）与 DRF 端共用
            user = ua.create_user_account(
                username=data["username"],
                password=data["password"],
                is_active=data.get("is_active", True),
                profile_data={
                    "real_name": data.get("real_name", ""),
                    "department": data.get("department", ""),
                    "position": data.get("position", ""),
                },
                roles=_parse_role_ids(request),
            )
            log_operation(
                request, AUDIT_MODULE, '新增', f'用户#{user.id}（{user.username}）',
                f'创建用户: {user.username}（{data.get("real_name") or "未填姓名"}），角色: {_user_roles_summary(user)}',
                after=snapshot(user),
            )
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
            before_snap = snapshot(target)
            old_roles = _user_roles_summary(target)
            data = form.cleaned_data
            fields = {"is_active": data.get("is_active", False)}
            # is_staff 仅超管可授：这里只在超管提交时才传入（非超管提交的会被
            # user_admin.update_user_account 内部丢弃，双保险）
            if request.user.is_superuser:
                fields["is_staff"] = data.get("is_staff", False)
            ua.update_user_account(
                request.user, target,
                fields=fields,
                profile_data={
                    "real_name": data.get("real_name", ""),
                    "department": data.get("department", ""),
                    "position": data.get("position", ""),
                },
                roles=_parse_role_ids(request),
            )
            log_operation(
                request, AUDIT_MODULE, '更新', f'用户#{target.id}（{target.username}）',
                f'编辑用户: {target.username}（角色 {old_roles} → {_user_roles_summary(target)}）',
                before=before_snap, after=snapshot(target),
            )
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


def _set_active(request, user_id, active, *, self_check=True, action='启停'):
    target, denied = _guarded_target(request, user_id)
    if denied is not None:
        return denied
    before_snap = snapshot(target)
    try:
        # 越权校验 + 禁止停用自己，与 DRF 端共用同一实现
        ua.set_user_active(request.user, target, active, forbid_self_disable=self_check)
    except ua.AccountRuleError as exc:
        messages.error(request, str(exc))
        return redirect("accounts:user_list")
    log_operation(
        request, AUDIT_MODULE, action, f'用户#{target.id}（{target.username}）',
        f'{action}: {target.username}（账号{"启用" if target.is_active else "停用"}）',
        before=before_snap, after=snapshot(target),
    )
    return target


@perm_required("system", "update")
@require_http_methods(["POST"])
def user_toggle_view(request, user_id):
    result = _set_active(
        request, user_id, request.POST.get("active") == "true",
        action='启用' if request.POST.get("active") == "true" else '停用')
    if not isinstance(result, User):
        return result
    messages.success(request, f"用户「{result.username}」已{'启用' if result.is_active else '停用'}。")
    return redirect("accounts:user_list")


@perm_required("system", "update")
@require_http_methods(["POST"])
def user_approve_view(request, user_id):
    result = _set_active(request, user_id, True, self_check=False, action='审核通过')
    if not isinstance(result, User):
        return result
    messages.success(request, f"已审核通过「{result.username}」，该账号现在可以登录。")
    return redirect("accounts:user_list")


@perm_required("system", "update")
@require_http_methods(["POST"])
def user_reject_view(request, user_id):
    result = _set_active(request, user_id, False, self_check=False, action='拒绝/停用')
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
            try:
                # 强度校验（带 user 上下文）+ 越权校验 + 落库，与 DRF 端共用
                ua.reset_user_password(request.user, target, form.cleaned_data["new_password"])
            except ua.AccountRuleError as exc:
                form.add_error("new_password", str(exc))
            else:
                log_operation(
                    request, AUDIT_MODULE, '重置密码', f'用户#{target.id}（{target.username}）',
                    f'管理员重置密码: {target.username}（该用户其它会话将被强制登出）',
                )
                # 密码指纹机制会使该用户其它会话在下次请求时被强制登出
                messages.success(request, f"「{target.username}」的密码已重置。")
                return redirect("accounts:user_list")
    else:
        form = AdminResetPasswordForm()
    return render(request, "accounts/user_reset_password.html", {"form": form, "target": target})


# ============================== 角色管理 ==============================

def _matrix(selected_codes):
    """构造权限矩阵渲染数据：固定 C/R/U/D 四列，模块不支持的动作为 None。

    CRUD 之外的高危附加动作（目前只有 system 的备份下载/数据恢复）单独放在
    `extra_actions`：模板里渲染成跨列子行，避免把所有模块的表格拉宽两列。
    """
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
        extra_actions = [
            {
                "label": ACTION_LABELS[a],
                "code": permission_code(module["key"], a),
                "checked": permission_code(module["key"], a) in selected_codes,
            }
            for a in module["actions"]
            if a not in columns
        ]
        matrix.append({
            "key": module["key"],
            "name": module["name"],
            "actions": actions,
            "extra_actions": extra_actions,
            "is_system": module["key"] == "system",
        })
    return matrix


@perm_required("system", "read")
def role_list_view(request):
    roles = Role.objects.prefetch_related("users").all().order_by("-is_builtin", "code")
    return render(request, "accounts/role_list.html", {"roles": roles})


def _save_role_permissions(role, raw_codes):
    """校验并整体替换角色权限；非法 code 返回错误信息字符串，成功返回 None。

    规则本体在 ``accounts.user_admin.save_role_permissions``（与 DRF 端共用），
    这里只是把异常转成页面需要的「错误文案」返回值。
    保留此薄封装是为了兼容页面侧既有调用点与潜在的外部引用。
    """
    try:
        ua.save_role_permissions(role, raw_codes)
    except ua.AccountRuleError as exc:
        return str(exc)
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
            try:
                # 编码唯一、禁止建内置角色、权限码合法性校验全在共用规则里，
                # 任一失败都在写入前抛出（不会留下半成品角色）
                role = ua.create_role(
                    code=form.cleaned_data["code"],
                    name=form.cleaned_data["name"],
                    description=form.cleaned_data.get("description", ""),
                    is_active=form.cleaned_data.get("is_active", False),
                    permission_codes=request.POST.getlist("perms"),
                )
            except ua.AccountRuleError as err:
                illegal_err = str(err)
                messages.error(request, illegal_err)
            else:
                log_operation(
                    request, AUDIT_MODULE, '新增', f'角色#{role.id}（{role.code}）',
                    f'创建角色: {role.name}（{role.code}），权限数: {role.permissions.count()}',
                    after=snapshot(role),
                )
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
            # 名称/说明/启停保持锁定 —— 两条不变式都在 update_role 里实现
            before_snap = snapshot(role)
            old_perm_count = role.permissions.count()
            try:
                ua.update_role(
                    role,
                    name=form.cleaned_data["name"],
                    description=form.cleaned_data.get("description", ""),
                    is_active=form.cleaned_data.get("is_active", False),
                    permission_codes=request.POST.getlist("perms"),
                )
            except ua.AccountRuleError as err:
                illegal_err = str(err)
                messages.error(request, illegal_err)
            else:
                log_operation(
                    request, AUDIT_MODULE, '更新', f'角色#{role.id}（{role.code}）',
                    f'编辑角色: {role.name}（权限 {old_perm_count} → {role.permissions.count()} 个）',
                    before=before_snap, after=snapshot(role),
                )
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
    role_name = role.name
    before_snap = snapshot(role)
    try:
        # 内置角色拒绝删除（与 DRF 端同一规则）
        user_count = ua.delete_role(role)
    except ua.AccountRuleError:
        return _forbidden(request)
    log_operation(
        request, AUDIT_MODULE, '删除', f'角色#{role_id}（{role.code}）',
        f'删除角色: {role_name}（原关联 {user_count} 个用户，删除前快照见 before）',
        before=before_snap,
    )
    messages.success(request, f"角色「{role_name}」已删除，已解除与 {user_count} 个用户的关联。")
    return redirect("accounts:role_list")

