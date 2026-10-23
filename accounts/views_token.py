"""长期 Token 管理页与签发接口。

- token_page_view：系统管理页，展示当前用户的历史 Token（system.update 权限）。
- obtain_api_token：``/api-token-auth/`` 签发接口。支持"已登录会话换取"与
  "用户名 + 口令换取"两种方式，供外部 Agent 在无人值守场景下自助取用。
"""

from django.contrib.auth import authenticate
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from records.audit import log_operation
from records.crud import _per_page

from .decorators import login_exempt, perm_required
from .models import TOKEN_TTL_DAYS, ApiToken
from .security import clear_failures, client_ip, is_locked, record_failure
from .services import has_perm

# 与侧边栏 {% can "system" "update" %} 入口保持同一把锁
MODULE = "system"
ACTION = "update"


def _fmt(dt):
    return timezone.localtime(dt).strftime("%Y-%m-%d %H:%M:%S") if dt else None


@perm_required(MODULE, ACTION)
def token_page_view(request):
    """长期 Token 管理页：签发入口 + 当前用户历史记录（分页）。"""
    # select_related("user")：状态列要读 user.password 判定"改密失效"，避免逐行 N+1
    queryset = (
        ApiToken.objects.filter(user=request.user)
        .select_related("user")
        .order_by("-created_at", "-id")
    )
    paginator = Paginator(queryset, _per_page(request))
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "accounts/api_token.html", {
        "page": page,
        "total": paginator.count,
        "token_ttl_days": TOKEN_TTL_DAYS,
    })


@perm_required(MODULE, ACTION)
@require_POST
def revoke_api_token(request, token_id):
    """撤销当前用户自己的一枚 Token（仅 POST，且仅限本人）。

    撤销后 is_active 置否，该 Token 立即失效，使用它的 Agent 调用将被 401。
    只按 (pk, user) 过滤，越权撤销他人 Token 一律落到 404（不泄露存在性）。
    """
    token = (
        ApiToken.objects.select_related("user")
        .filter(pk=token_id, user=request.user)
        .first()
    )
    if token is None:
        raise Http404("Token 不存在")
    if token.is_active:
        token.is_active = False
        token.save(update_fields=["is_active"])
        try:
            log_operation(
                request, "系统管理", "删除", f"长期Token#{token.pk}",
                f"撤销用户 {request.user.get_username()} 的长期 Token",
            )
        except Exception:  # 留痕失败不影响撤销主流程
            import logging

            logging.getLogger(__name__).exception("长期 Token 撤销留痕写入失败")
    return redirect("accounts:api_token")


@login_exempt
@api_view(["POST"])
@permission_classes([AllowAny])
def obtain_api_token(request):
    """签发一枚长期 Token。

    - 已登录（会话或既有 Token）：直接为当前用户签发；
    - 未登录：需提供 username / password，校验通过后签发。
    两种情况均要求用户拥有 system.update 权限（超管恒通过）。
    """
    user = request.user if getattr(request.user, "is_authenticated", False) else None

    if user is None:
        username = str(request.data.get("username") or "").strip()
        password = request.data.get("password") or ""
        if not username or not password:
            return Response(
                {"detail": "请提供用户名与密码。"}, status=status.HTTP_400_BAD_REQUEST
            )
        ip = client_ip(request)
        # 复用登录失败锁定，避免此处成为绕过锁定的口令爆破通道
        locked, _left = is_locked(ip, username)
        if locked:
            return Response(
                {"detail": "登录失败次数过多，账号已锁定 15 分钟，请稍后再试。"},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        user = authenticate(request, username=username, password=password)
        if user is None:
            record_failure(ip, username)
            return Response(
                {"detail": "用户名或密码错误。"}, status=status.HTTP_401_UNAUTHORIZED
            )
        clear_failures(ip, username)

    if not user.is_active:
        return Response({"detail": "账户已停用。"}, status=status.HTTP_403_FORBIDDEN)
    if not has_perm(user, MODULE, ACTION):
        return Response(
            {"detail": "无权生成长期 Token。"}, status=status.HTTP_403_FORBIDDEN
        )

    token = ApiToken.issue(user, ip=client_ip(request))
    try:
        log_operation(
            request, "系统管理", "新增", f"长期Token#{token.pk}",
            f"为用户 {user.get_username()} 签发长期 Token（前缀 {token.key_prefix}）",
        )
    except Exception:  # 留痕失败不影响签发主流程
        import logging

        logging.getLogger(__name__).exception("长期 Token 操作留痕写入失败")

    # 明文只在这一次响应里出现（P1-2）。此后历史列表只能看到 key_prefix 掩码，
    # 丢钥只能重新签发，换取的是「备份包外流不再等于凭据外流」。
    return Response({
        "id": token.pk,
        "key": token.raw_key,
        "key_prefix": token.key_prefix,
        "created_at": _fmt(token.created_at),
        "expires_at": _fmt(token.expires_at),
        "status": token.status_code,
        "status_text": token.status_text,
    }, status=status.HTTP_201_CREATED)
