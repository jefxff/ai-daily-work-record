"""账户 / 通知 / 系统管理的 DRF API（补齐页面无 API 的缺口）。

设计原则（与页面端对齐）：
- 权限：复用 accounts.permissions_drf.ModuleRbacPermission（system 模块 C/R/U/D），
  自定义 action 通过 ``custom_action_perms`` 声明映射，未映射一律拒绝；
  通知的"本人数据"类动作在视图内做行级校验。
- 留痕：所有写操作调用 records.audit.log_operation，模块名与页面端一致。
- 状态码语义与页面端一致：业务校验失败返回 422，无权限 403。
"""
import logging

from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from rest_framework import serializers, status, viewsets, mixins
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts import user_admin as ua
from accounts.models import ApiToken, LoginLog, Role, UserProfile
from accounts.permissions_drf import ModuleRbacPermission
from accounts.registry import all_permission_codes
from accounts.services import has_perm
from notifications import scheduler, services as notify_services
from notifications.models import (
    Notification,
    NotificationPreference,
    ScheduledTask,
    TaskRunLog,
    TEMPLATE_CATEGORIES,
)
from records.audit import log_operation, snapshot

logger = logging.getLogger(__name__)

AUDIT_MODULE = "系统管理"          # 账户/角色/Token 留痕模块名（与页面端一致）
NOTIFY_MODULE = "通知中心"         # 通知写操作留痕模块名（与页面端一致）

_has_perm = has_perm  # 本模块内的简写（超管在 has_perm 内全部放行）


def _require(request, module, action):
    """视图内显式 RBAC 判定；未授权抛 403。"""
    if not _has_perm(request.user, module, action):
        raise PermissionDenied("您没有执行该操作的权限。")


# ============================== 账户管理 ==============================

class UserProfileInlineSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserProfile
        fields = ["real_name", "department", "position"]


class RoleLiteSerializer(serializers.ModelSerializer):
    permission_codes = serializers.SerializerMethodField()

    class Meta:
        model = Role
        fields = [
            "id", "code", "name", "description", "is_active",
            "is_builtin", "permission_codes", "created_at", "updated_at",
        ]
        read_only_fields = ["is_builtin", "created_at", "updated_at"]

    def get_permission_codes(self, obj):
        return list(obj.permissions.values_list("code", flat=True))

    def to_internal_value(self, data):
        """接受 permission_codes（权限码列表）整体替换角色权限。

        只有请求里**显式带了** permission_codes 才注入，否则 PATCH 只改名称会被
        误判成「权限清空」（原实现无条件注入 []，等于每次局部更新都清权限）。
        """
        ret = super().to_internal_value(data)
        if "permission_codes" in data:
            ret["permission_codes"] = data.get("permission_codes") or []
        return ret

    def validate_permission_codes(self, codes):
        valid = set(all_permission_codes())
        illegal = {c for c in codes if c} - valid
        if illegal:
            raise serializers.ValidationError(
                f"非法权限码: {', '.join(sorted(illegal))}")
        return [c for c in codes if c]


class UserAdminSerializer(serializers.ModelSerializer):
    profile = UserProfileInlineSerializer(required=False)
    roles = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Role.objects.filter(is_active=True), required=False, source="rbac_roles")
    role_names = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "username", "email", "is_active", "is_staff",
            "last_login", "profile", "roles", "role_names",
        ]
        read_only_fields = ["last_login"]

    def get_role_names(self, obj):
        return list(obj.rbac_roles.values_list("name", flat=True))

    def create(self, validated_data):
        profile_data = validated_data.pop("profile", {})
        roles = validated_data.pop("rbac_roles", [])
        password = self.context["request"].data.get("password")
        if not password:
            raise ValidationError({"password": "创建用户必须提供初始密码。"})
        # 创建规则与页面端共用（档案必建、角色挂载、is_active 归一）
        return ua.create_user_account(
            username=validated_data["username"],
            password=password,
            email=validated_data.get("email", ""),
            is_active=validated_data.get("is_active", True),
            profile_data=profile_data,
            roles=roles,
        )

    def update(self, instance, validated_data):
        profile_data = validated_data.pop("profile", None)
        roles = validated_data.pop("rbac_roles", None)
        # is_staff 仅超管可授：由共用规则内部丢弃非超管提交的该字段
        return ua.update_user_account(
            self.context["request"].user, instance,
            fields=validated_data, profile_data=profile_data, roles=roles,
        )


class UserAdminViewSet(viewsets.ModelViewSet):
    """用户管理 API：GET/POST/PATCH/DELETE + toggle/approve/reject/reset-password。

    页面端不提供用户删除（只停用），这里同样不开放 destroy。
    非超管不能操作超管账号（与页面端 _guarded_target 一致）。
    """
    permission_classes = [IsAuthenticated, ModuleRbacPermission]
    perm_module = "system"
    custom_action_perms = {
        "toggle": "update", "approve": "update",
        "reject": "update", "reset_password": "update",
    }
    queryset = User.objects.select_related("profile").prefetch_related("rbac_roles") \
        .order_by("-is_superuser", "-id")
    serializer_class = UserAdminSerializer
    filterset_fields = ["is_active", "is_staff"]
    search_fields = ["username", "email", "profile__real_name"]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_object(self):
        """取单个用户时统一做「非超管不得操作超管」校验。

        放在 get_object 而不是各个 action 里：retrieve / update / partial_update /
        toggle / approve / reject / reset-password 全都走这里，新增 action 时
        不会漏掉这道检查（原实现只在部分 action 里调用，PATCH 更新可绕过）。
        """
        target = super().get_object()
        # 与页面端 _guarded_target 共用同一判定（accounts.user_admin）
        try:
            ua.check_can_manage_user(self.request.user, target)
        except ua.AccountRuleError as exc:
            raise PermissionDenied(str(exc)) from exc
        return target

    def _guarded(self, pk):
        """兼容既有调用点：pk 由 get_object 从 URL 解析，这里只需触发校验。"""
        return self.get_object()

    def perform_create(self, serializer):
        user = serializer.save()
        role_names = list(user.rbac_roles.values_list("name", flat=True))
        log_operation(
            self.request, AUDIT_MODULE, "新增", f"用户#{user.id}（{user.username}）",
            f"[API] 创建用户: {user.username}，角色: {'、'.join(role_names) or '（无角色）'}",
            after=snapshot(user),
        )

    def perform_update(self, serializer):
        before = snapshot(serializer.instance)
        user = serializer.save()
        log_operation(
            self.request, AUDIT_MODULE, "更新", f"用户#{user.id}（{user.username}）",
            f"[API] 编辑用户: {user.username}",
            before=before, after=snapshot(user),
        )

    @action(detail=True, methods=["post"], url_path="toggle")
    def toggle(self, request, pk=None):
        """启用/停用用户。body: {"active": true|false}"""
        target = self._guarded(pk)
        active = bool(request.data.get("active", not target.is_active))
        before = snapshot(target)
        try:
            # 停用自己必须被拒（页面与 API 同一条规则）
            ua.set_user_active(request.user, target, active)
        except ua.AccountRuleError as exc:
            return Response({"ok": False, "message": str(exc)},
                            status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        log_operation(
            request, AUDIT_MODULE, "启用" if active else "停用",
            f"用户#{target.id}（{target.username}）",
            f"[API] {'启用' if active else '停用'}用户: {target.username}",
            before=before, after=snapshot(target),
        )
        return Response({"ok": True, "is_active": target.is_active})

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """审核通过注册申请（启用账号）。"""
        return self._set_active_flag(request, pk, True, "审核通过")

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        """拒绝注册 / 停用账号。"""
        return self._set_active_flag(request, pk, False, "拒绝/停用")

    def _set_active_flag(self, request, pk, active, label):
        target = self._guarded(pk)
        before = snapshot(target)
        try:
            # 审核动作允许操作自己（与页面端 self_check=False 对齐）
            ua.set_user_active(request.user, target, active, forbid_self_disable=False)
        except ua.AccountRuleError as exc:
            return Response({"ok": False, "message": str(exc)},
                            status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        log_operation(
            request, AUDIT_MODULE, label, f"用户#{target.id}（{target.username}）",
            f"[API] {label}: {target.username}（账号{'启用' if active else '停用'}）",
            before=before, after=snapshot(target),
        )
        return Response({"ok": True, "is_active": target.is_active})

    @action(detail=True, methods=["post"], url_path="reset-password")
    def reset_password(self, request, pk=None):
        """管理员重置密码。body: {"new_password": "...", "confirm": "..."}"""
        target = self._guarded(pk)
        new_pwd = request.data.get("new_password") or ""
        confirm = request.data.get("confirm") or ""
        if not new_pwd:
            return Response({"ok": False, "message": "新密码不能为空。"},
                            status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        if new_pwd != confirm:
            return Response({"ok": False, "message": "两次输入的密码不一致。"},
                            status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        try:
            # 强度校验 + 越权校验 + 落库，与页面端共用同一实现
            ua.reset_user_password(request.user, target, new_pwd)
        except ua.AccountRuleError as exc:
            return Response({"ok": False, "message": str(exc)},
                            status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        log_operation(
            request, AUDIT_MODULE, "重置密码", f"用户#{target.id}（{target.username}）",
            f"[API] 管理员重置密码: {target.username}（该用户其它会话将被强制登出）",
        )
        return Response({"ok": True, "message": "密码已重置。"})


class RoleAdminViewSet(viewsets.ModelViewSet):
    """角色管理 API：CRUD + 权限码整体替换。内置角色不允许删除。"""
    permission_classes = [IsAuthenticated, ModuleRbacPermission]
    perm_module = "system"
    queryset = Role.objects.prefetch_related("permissions").order_by("-is_builtin", "code")
    serializer_class = RoleLiteSerializer
    search_fields = ["code", "name"]

    def perform_create(self, serializer):
        data = serializer.validated_data
        codes = data.pop("permission_codes", [])
        try:
            # 编码唯一 + 强制 is_builtin=False + 权限码合法性，全部走共用规则，
            # 任一失败都在写入前抛出（不会留下半成品角色）
            role = ua.create_role(
                code=data.get("code"),
                name=data.get("name"),
                description=data.get("description", ""),
                is_active=data.get("is_active", False),
                permission_codes=codes,
            )
        except ua.AccountRuleError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        # 规则层直接建实例（不经过 serializer.save），必须回填 instance，
        # 否则 DRF 会用 validated_data 当 instance 去序列化响应而报错
        serializer.instance = role
        log_operation(
            self.request, AUDIT_MODULE, "新增", f"角色#{role.id}（{role.code}）",
            f"[API] 创建角色: {role.name}（{role.code}），权限数: {role.permissions.count()}",
            after=snapshot(role),
        )

    def perform_update(self, serializer):
        role = serializer.instance
        before = snapshot(role)
        old_count = role.permissions.count()
        # code 永不修改（与页面端一致）；permission_codes 未提供时保持原权限不动
        serializer.validated_data.pop("code", None)
        codes = serializer.validated_data.pop("permission_codes", None)
        try:
            ua.update_role(
                role,
                name=serializer.validated_data.pop("name", None),
                description=serializer.validated_data.pop("description", None),
                is_active=serializer.validated_data.pop("is_active", None),
                permission_codes=codes,
            )
        except ua.AccountRuleError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        log_operation(
            self.request, AUDIT_MODULE, "更新", f"角色#{role.id}（{role.code}）",
            f"[API] 编辑角色: {role.name}（权限 {old_count} → {role.permissions.count()} 个）",
            before=before, after=snapshot(role),
        )

    def perform_destroy(self, instance):
        user_count = instance.users.count()
        before = snapshot(instance)
        name = instance.name
        try:
            ua.delete_role(instance)
        except ua.AccountRuleError as exc:
            raise ValidationError({"detail": str(exc)}) from exc
        log_operation(
            self.request, AUDIT_MODULE, "删除", f"角色#{before.get('id')}（{before.get('code')}）",
            f"[API] 删除角色: {name}（原关联 {user_count} 个用户，删除前快照见 before）",
            before=before,
        )


class LoginLogViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                      viewsets.GenericViewSet):
    """登录日志（只读）。支持 ?username= 模糊与 ?result=success|failed 筛选。"""
    permission_classes = [IsAuthenticated, ModuleRbacPermission]
    perm_module = "system"
    queryset = LoginLog.objects.select_related("user").all()
    search_fields = ["username", "ip"]
    filterset_fields = ["success"]

    def get_serializer_class(self):
        return None  # 使用 to_representation 手写序列化

    def list(self, request, *args, **kwargs):
        qs = self.filter_queryset(self.get_queryset())
        username = (request.query_params.get("username") or "").strip()
        result = request.query_params.get("result", "")
        if username:
            qs = qs.filter(username__icontains=username)
        if result == "success":
            qs = qs.filter(success=True)
        elif result == "failed":
            qs = qs.filter(success=False)
        page = self.paginate_queryset(qs)
        data = [_loginlog_repr(x) for x in (page or qs[:100])]
        if page is not None:
            return self.get_paginated_response(data)
        return Response(data)

    def retrieve(self, request, *args, **kwargs):
        return Response(_loginlog_repr(self.get_object()))


def _loginlog_repr(log):
    return {
        "id": log.id,
        "username": log.username,
        "login_type": log.login_type,
        "success": log.success,
        "ip": getattr(log, "ip", ""),
        "user_agent": getattr(log, "user_agent", ""),
        "user": log.user.username if log.user_id else None,
        "created_at": timezone.localtime(log.created_at).isoformat() if log.created_at else None,
    }


class SessionAPIView(APIView):
    """我的会话管理 API：GET 列表 / POST revoke 撤销指定会话 / POST revoke-others。

    仅作用于本人会话（行级校验），声明 perm_explicit_checks 供权限覆盖守卫识别。
    """
    permission_classes = [IsAuthenticated]
    perm_explicit_checks = True

    def get(self, request):
        return Response(_session_list(request.user, request.session.session_key))

    def post(self, request, action=None):
        if action == "revoke":
            key = request.data.get("session_key") or ""
            if not key or key == request.session.session_key:
                return Response({"ok": False, "message": "不能撤销当前会话（请走登出）。"},
                                status=status.HTTP_422_UNPROCESSABLE_ENTITY)
            # 仅允许撤销本人的会话
            owner_ok = False
            for s in Session.objects.filter(session_key=key):
                data = s.get_decoded()
                owner_ok = str(data.get("_auth_user_id")) == str(request.user.pk)
            if not owner_ok:
                raise PermissionDenied("只能撤销本人的会话。")
            Session.objects.filter(session_key=key).delete()
            log_operation(
                request, AUDIT_MODULE, "撤销会话",
                f"用户#{request.user.id}（{request.user.username}）",
                f"[API] 下线指定会话: …{key[-8:]}",
            )
            return Response({"ok": True})
        if action == "revoke-others":
            current = request.session.session_key
            revoked = 0
            for s in Session.objects.filter(expire_date__gt=timezone.now()):
                if s.session_key == current:
                    continue
                data = s.get_decoded()
                if data.get("_auth_user_id") and int(data["_auth_user_id"]) == request.user.pk:
                    s.delete()
                    revoked += 1
            if revoked:
                log_operation(
                    request, AUDIT_MODULE, "撤销会话",
                    f"用户#{request.user.id}（{request.user.username}）",
                    f"[API] 下线除当前会话外的全部会话（共 {revoked} 个）",
                )
            return Response({"ok": True, "revoked": revoked})
        return Response({"detail": "未知操作"}, status=status.HTTP_404_NOT_FOUND)


def _session_list(user, current_key):
    items = []
    for s in Session.objects.filter(expire_date__gt=timezone.now()):
        data = s.get_decoded()
        if data.get("_auth_user_id") is None or int(data["_auth_user_id"]) != user.pk:
            continue
        items.append({
            "session_key": s.session_key,
            "is_current": s.session_key == current_key,
            "login_ts": data.get("login_ts"),
            "login_ip": data.get("login_ip", ""),
            "login_ua": data.get("login_ua", ""),
            "expire_at": s.expire_date.isoformat(),
        })
    items.sort(key=lambda x: (not x["is_current"], str(x["login_ts"])))
    return items


class ApiTokenViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                      viewsets.GenericViewSet):
    """我的长期 Token：列表 / 详情 / 撤销（revoke）。明文 key 不落库，永不回显。

    仅本人数据（get_queryset 按当前用户过滤），行级校验，豁免单一 perm_module 声明。
    """
    permission_classes = [IsAuthenticated]
    perm_explicit_checks = True
    queryset = ApiToken.objects.all()

    def get_queryset(self):
        return ApiToken.objects.filter(user=self.request.user)

    def list(self, request, *args, **kwargs):
        data = [_token_repr(t) for t in self.get_queryset()]
        return Response(data)

    def retrieve(self, request, *args, **kwargs):
        return Response(_token_repr(self.get_object()))

    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):
        token = self.get_object()
        if not token.is_active:
            return Response({"ok": False, "message": "该 Token 已撤销。"},
                            status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        token.is_active = False
        token.save(update_fields=["is_active"])
        log_operation(
            request, AUDIT_MODULE, "撤销Token", f"Token#{token.id}（{token.user.username}）",
            f"[API] 撤销长期 Token: {token.user.username}（前缀 {token.key_prefix}…）",
        )
        return Response({"ok": True})


def _token_repr(t):
    status_label = "有效"
    if not t.is_active:
        status_label = "已撤销"
    elif t.is_expired:
        status_label = "已过期"
    return {
        "id": t.id,
        "key_prefix": t.key_prefix,
        "status": status_label,
        "created_at": t.created_at.isoformat(),
        "expires_at": t.expires_at.isoformat() if t.expires_at else None,
        "created_ip": t.created_ip,
    }


# ============================== 通知中心 ==============================

def _notification_repr(n):
    return {
        "id": n.id,
        "title": n.title,
        "content": n.content,
        "link": n.link,
        "priority": n.priority,
        "category": n.category,
        "category_label": n.get_category_display(),
        "recipient": n.recipient.username if n.recipient_id else None,
        "created_by": n.created_by.username if n.created_by_id else None,
        "is_read": n.is_read,
        "status": n.status,
        "batch_id": n.batch_id,
        "agg_count": n.agg_count,
        "created_at": timezone.localtime(n.created_at).isoformat(),
        "sent_at": timezone.localtime(n.sent_at).isoformat() if n.sent_at else None,
    }


class NotificationViewSet(viewsets.ViewSet):
    """通知 API：list / retrieve / mark-read / mark-all-read / revoke / revoke-log。

    list 与已读动作仅限本人数据；revoke 与 revoke-log 要求 system.update（或触发人本人）。

    权限为行级/跨模块混合校验（notifications.read 本人数据 + system.update 撤回），
    无法用单一 perm_module 表达，故显式声明 perm_explicit_checks 并在视图内用
    _require()/services 层校验（AC-12 覆盖守卫认可该豁免标记）。
    """
    permission_classes = [IsAuthenticated]
    perm_explicit_checks = True

    def list(self, request):
        qs = notify_services._my_notifications(request.user).exclude(
            status__in=[Notification.Status.REVOKED, Notification.Status.EXPIRED])
        unread_only = request.query_params.get("unread") == "1"
        if unread_only:
            qs = qs.filter(is_read=False)
        page = self._paginate(request, qs)
        data = [_notification_repr(n) for n in page[0]]
        return Response({"ok": True, "count": len(data), "unread": notify_services.unread_count(request.user),
                         "items": data, "page": page[1], "has_next": page[2]})

    def retrieve(self, request, pk=None):
        n = notify_services._get_my_notification(request, pk)
        return Response({"ok": True, "item": _notification_repr(n)})

    @action(detail=True, methods=["post"], url_path="mark-read")
    def mark_read(self, request, pk=None):
        """标记单条已读（仅本人）。"""
        n = notify_services._get_my_notification(request, pk)
        notify_services.mark_read(n)
        return Response({"ok": True})

    @action(detail=False, methods=["post"], url_path="mark-all-read")
    def mark_all_read(self, request):
        count = notify_services.mark_all_read(request.user)
        return Response({"ok": True, "count": count})

    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):
        """撤回通知：system.update 或触发人本人；状态规则由 services.revoke 把关。"""
        n = Notification.objects.filter(pk=pk).first()
        if n is None:
            return Response({"ok": False, "message": "通知不存在。"}, status=404)
        count = notify_services.revoke(n, request.user)
        if count:
            log_operation(
                request, NOTIFY_MODULE, "撤回", f"通知#{n.pk}（{n.title[:50]}）",
                f"[API] 撤回通知「{n.title[:80]}」，共撤回 {count} 条",
            )
            return Response({"ok": True, "revoked": count})
        return Response({"ok": False, "message": "无权限或该通知当前状态不可撤回"},
                        status=status.HTTP_422_UNPROCESSABLE_ENTITY)

    @action(detail=False, methods=["get"], url_path="revoke-log")
    def revoke_log(self, request):
        """撤回记录（按批次聚合）。要求 system.update 权限。"""
        _require(request, "system", "update")
        from django.db.models import Count, Max, Q
        base = Notification.objects.filter(status=Notification.Status.REVOKED)
        items = []
        for row in base.exclude(batch_id="").values("batch_id").annotate(
                title=Max("title"), sent_at=Max("sent_at"), created_at=Max("created_at"),
                total=Count("id"), read_count=Count("id", filter=Q(is_read=True)),
        ).order_by("-created_at"):
            items.append({**row, "unread_count": row["total"] - row["read_count"]})
        return Response({"ok": True, "items": items})

    @staticmethod
    def _paginate(request, qs, per_page=20):
        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except (TypeError, ValueError):
            page = 1
        total = qs.count()
        items = list(qs[(page - 1) * per_page: page * per_page])
        import math
        return items, page, page * per_page < total


class NotificationPreferenceAPIView(APIView):
    """我的通知偏好：GET 读取 / PUT 保存（仅本人），行级校验。"""
    permission_classes = [IsAuthenticated]
    perm_explicit_checks = True

    def get(self, request):
        prefs = notify_services.get_prefs(request.user)
        return Response({"ok": True, "channel_prefs": prefs.channel_prefs,
                         "agg_window_minutes": prefs.agg_window_minutes,
                         "quiet_start": prefs.quiet_start, "quiet_end": prefs.quiet_end})

    def put(self, request):
        prefs = notify_services.get_prefs(request.user)
        body = request.data or {}
        channel_prefs = body.get("channel_prefs")
        if channel_prefs is not None:
            if not isinstance(channel_prefs, dict):
                return Response({"ok": False, "message": "channel_prefs 必须为对象。"},
                                status=status.HTTP_422_UNPROCESSABLE_ENTITY)
            valid_keys = {key for key, _label in TEMPLATE_CATEGORIES}
            prefs.channel_prefs = {
                key: {"email": bool((channel_prefs.get(key) or {}).get("email"))}
                for key in valid_keys
            }
        try:
            prefs.agg_window_minutes = max(0, min(120, int(body.get("agg_window_minutes", prefs.agg_window_minutes))))
        except (TypeError, ValueError):
            pass
        prefs.quiet_start = body.get("quiet_start") or None
        prefs.quiet_end = body.get("quiet_end") or None
        prefs.save()
        log_operation(
            request, NOTIFY_MODULE, "偏好修改",
            f"用户#{request.user.id}（{request.user.username}）",
            f"[API] 修改通知偏好: 邮件开关={prefs.channel_prefs}, 聚合窗口={prefs.agg_window_minutes}分钟",
        )
        return Response({"ok": True})


class AnnounceAPIView(APIView):
    """发布公告：POST system.create。body: {title, content, link?, priority?, expires_days?}

    权限在视图内 _require(request, "system", "create")；声明 perm_explicit_checks 供守卫识别。
    """
    permission_classes = [IsAuthenticated]
    perm_explicit_checks = True

    def post(self, request):
        _require(request, "system", "create")
        title = (request.data.get("title") or "").strip()
        content = (request.data.get("content") or "").strip()
        if not title:
            return Response({"ok": False, "message": "请填写公告标题。"},
                            status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        if not content:
            return Response({"ok": False, "message": "请填写公告内容。"},
                            status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        try:
            expires_days = int(request.data.get("expires_days") or 0) or None
        except (TypeError, ValueError):
            expires_days = None
        count = notify_services.announce(
            title, content, created_by=request.user,
            link=(request.data.get("link") or "").strip(),
            priority=request.data.get("priority") or "normal",
            expires_days=expires_days,
        )
        log_operation(
            request, NOTIFY_MODULE, "发布公告", f"公告「{title[:80]}」",
            f"[API] 发布系统公告「{title[:80]}」，推送给 {count} 位用户",
        )
        return Response({"ok": True, "recipients": count})


# ============================== 系统监控（只读） ==============================

class SystemHealthAPIView(APIView):
    """系统健康 API（只读，system.read）：健康探测 + 进程心跳 + 磁盘概要。

    权限在视图内 _require(request, "system", "read")。
    """
    permission_classes = [IsAuthenticated]
    perm_explicit_checks = True

    def get(self, request):
        import os
        import shutil
        from django.conf import settings
        from notifications import views_health as vh
        from notifications.views_health import _CHECK_LABELS
        from sysbackup.models import BackupOperationLog
        from work_record.health import probe_health

        _require(request, "system", "read")
        checks, healthy = probe_health()
        usage = shutil.disk_usage(str(settings.BASE_DIR))
        db_path = os.path.join(str(settings.BASE_DIR), "db.sqlite3")
        return Response({
            "ok": True,
            "healthy": healthy,
            "checks": [
                {"key": key, "label": _CHECK_LABELS.get(key, key), "value": checks.get(key)}
                for key in ("database", "migrations", "log_dir")
            ],
            "heartbeats": [
                {"label": "后台发送进程（worker）", "alive": vh.scheduler.worker_alive(),
                 "time": vh._fmt_heartbeat(vh.scheduler.heartbeat_ts("worker"))},
                {"label": "定时调度进程（scheduler）", "alive": vh.scheduler.scheduler_alive(),
                 "time": vh._fmt_heartbeat(vh.scheduler.heartbeat_ts("scheduler"))},
            ],
            "disk": {
                "total": vh._human_size(usage.total),
                "used": vh._human_size(usage.used),
                "free": vh._human_size(usage.free),
                "percent": round(usage.used / usage.total * 100, 1) if usage.total else 0,
            },
            "db_size": vh._human_size(os.path.getsize(db_path)) if os.path.isfile(db_path) else None,
            "recent_backup_ops": [
                {"op_type": x.op_type, "file_name": x.file_name, "ok": x.ok,
                 "created_at": timezone.localtime(x.created_at).isoformat()}
                for x in BackupOperationLog.objects.all()[:20]
            ],
        })


class SystemPerfAPIView(APIView):
    """性能监控 API（只读，system.read）：?days=1~90，返回请求量 / P95 / 慢请求 Top。

    权限在视图内 _require(request, "system", "read")。
    """
    permission_classes = [IsAuthenticated]
    perm_explicit_checks = True

    def get(self, request):
        from datetime import timedelta
        from django.conf import settings
        from django.db.models import Avg, Count, Max, Sum
        from notifications.models import RequestMetric
        from notifications.views_metrics import (
            _clamp_days, _percentile_value, TOP_N,
        )

        _require(request, "system", "read")
        days = _clamp_days(request.query_params.get("days"))
        since = timezone.now() - timedelta(days=days)
        qs = RequestMetric.objects.filter(created_at__gte=since)
        slow_ms = getattr(settings, "REQUEST_METRICS_SLOW_MS", 300)
        agg = qs.aggregate(avg_ms=Avg("duration_ms"), max_ms=Max("duration_ms"))
        summary = {
            "total": qs.count(),
            "avg_ms": round(agg["avg_ms"] or 0),
            "p95_ms": _percentile_value(qs, "duration_ms", 95) or 0,
            "max_ms": agg["max_ms"] or 0,
            "slow_count": qs.filter(duration_ms__gt=slow_ms).count(),
            "error_count": qs.filter(status_code__gte=500).count(),
            "slow_query_count": qs.exclude(slow_query="").count(),
        }
        top_paths = list(qs.values("path", "method").annotate(
            cnt=Count("id"), avg_ms=Avg("duration_ms"), max_ms=Max("duration_ms"),
        ).order_by("-cnt")[:TOP_N])
        slow_paths = list(qs.values("path", "method").annotate(
            cnt=Count("id"), avg_ms=Avg("duration_ms"), max_ms=Max("duration_ms"),
        ).order_by("-avg_ms")[:TOP_N])
        return Response({"ok": True, "days": days, "summary": summary,
                         "top_paths": top_paths, "slow_paths": slow_paths})


# ============================== 定时任务（超管） ==============================

class ScheduledTaskViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin,
                           viewsets.GenericViewSet):
    """定时任务 API：list/retrieve 需 system.read；config/run 仅超管（与页面端一致）。

    权限为显式校验（system.read 列表 + 超管写操作），声明豁免标记供 AC-12 守卫识别。
    """
    permission_classes = [IsAuthenticated]
    perm_explicit_checks = True
    queryset = ScheduledTask.objects.all()
    custom_action_perms = {"config": "update", "run": "update"}

    def _require_superuser(self):
        if not self.request.user.is_superuser:
            raise PermissionDenied("仅超级管理员可操作定时任务配置。")

    def list(self, request, *args, **kwargs):
        _require(request, "system", "read")
        data = [_task_repr(t) for t in self.get_queryset()]
        return Response({"ok": True, "items": data})

    def retrieve(self, request, *args, **kwargs):
        _require(request, "system", "read")
        return Response({"ok": True, "item": _task_repr(self.get_object())})

    @action(detail=True, methods=["post"], url_path="config")
    def config(self, request, pk=None):
        """修改任务配置。body: {enabled, trigger_type, run_time?, interval_minutes?, params?}"""
        self._require_superuser()
        task = self.get_object()
        errors = []
        enabled = bool(request.data.get("enabled"))
        trigger_type = request.data.get("trigger_type")
        run_time = None
        interval_minutes = None
        if trigger_type == ScheduledTask.TriggerType.DAILY:
            raw = (request.data.get("run_time") or "").strip()
            try:
                from datetime import time as _time
                run_time = _time.fromisoformat(raw)
            except ValueError:
                errors.append("请提供有效的每日执行时间（HH:MM）")
        elif trigger_type == ScheduledTask.TriggerType.INTERVAL:
            try:
                interval_minutes = int(request.data.get("interval_minutes", 0))
            except (TypeError, ValueError):
                interval_minutes = 0
            if not 1 <= interval_minutes <= 7 * 24 * 60:
                errors.append("间隔分钟数需在 1~10080 之间")
        else:
            errors.append("触发方式无效")
            trigger_type = None
        params = request.data.get("params")
        if params is not None and not isinstance(params, dict):
            errors.append("params 必须为对象")
        if errors:
            return Response({"ok": False, "message": "；".join(errors)},
                            status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        task.enabled = enabled
        task.trigger_type = trigger_type
        task.run_time = run_time
        task.interval_minutes = interval_minutes
        if params is not None:
            task.params = params
        task.save(update_fields=["enabled", "trigger_type", "run_time",
                                 "interval_minutes", "params", "updated_at"])
        log_operation(
            request, NOTIFY_MODULE, "任务配置", f"定时任务#{task.pk}（{task.code}）",
            f"[API] 修改定时任务「{task.name}」配置: 启用={enabled}, 触发={trigger_type}, 参数={task.params}",
        )
        return Response({"ok": True, "item": _task_repr(task)})

    @action(detail=True, methods=["post"])
    def run(self, request, pk=None):
        """手动立即执行一次。"""
        self._require_superuser()
        task = self.get_object()
        executed, ok, message = scheduler.run_task(task, source="manual")
        if not executed:
            return Response({"ok": False, "message": message},
                            status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        log_operation(
            request, NOTIFY_MODULE, "手动执行", f"定时任务#{task.pk}（{task.code}）",
            f"[API] 手动执行定时任务「{task.name}」: {'成功' if ok else '失败'}",
        )
        return Response({"ok": bool(ok), "message": message})


def _task_repr(t):
    return {
        "id": t.id, "code": t.code, "name": t.name, "description": t.description,
        "enabled": t.enabled, "trigger_type": t.trigger_type,
        "run_time": t.run_time and t.run_time.isoformat(),
        "interval_minutes": t.interval_minutes, "params": t.params,
        "last_run_at": timezone.localtime(t.last_run_at).isoformat() if t.last_run_at else None,
        "last_status": t.last_status, "last_message": t.last_message[:200],
        "next_run_at": (scheduler.next_run_at(t).isoformat()
                        if scheduler.next_run_at(t) else None),
    }


class TaskRunLogAPIView(APIView):
    """任务执行历史（只读，system.read）。?task=<id>&status=success|fail&page=N

    权限在视图内 _require(request, "system", "read")。
    """
    permission_classes = [IsAuthenticated]
    perm_explicit_checks = True

    def get(self, request):
        _require(request, "system", "read")
        logs = TaskRunLog.objects.select_related("task")
        task_filter = request.query_params.get("task", "")
        status_filter = request.query_params.get("status", "")
        if task_filter.isdigit():
            logs = logs.filter(task_id=int(task_filter))
        if status_filter in ("success", "fail"):
            logs = logs.filter(status=status_filter)
        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except (TypeError, ValueError):
            page = 1
        per_page = 20
        total = logs.count()
        items = [{
            "id": x.id,
            "task": {"id": x.task_id, "code": x.task.code, "name": x.task.name},
            "source": x.source,
            "status": x.status,
            "message": x.message[:300],
            "started_at": timezone.localtime(x.started_at).isoformat() if x.started_at else None,
            "finished_at": timezone.localtime(x.finished_at).isoformat() if x.finished_at else None,
        } for x in logs[(page - 1) * per_page: page * per_page]]
        return Response({"ok": True, "items": items, "page": page,
                         "total": total, "has_next": page * per_page < total})
