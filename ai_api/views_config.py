"""AI 配置页面（系统管理，system.update 权限）。

- ai_config_view：GET 展示当前配置与表单；
- ai_config_save：POST 保存并立即生效（保存后周报 / 年度总结即按新配置润色）；
- ai_config_reset：POST 重置（清除配置，回到未启用状态）；
- ai_config_test：POST 用提交或已存参数做一次连通性测试，返回 JSON 供页面 toast。

所有写操作均记录操作留痕；留痕失败仅记日志，不影响主流程。
"""

import ipaddress
import logging
from urllib.parse import urlsplit

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from accounts.decorators import perm_required
from records.audit import log_operation

from .client import test_connection
from .models import AiProviderConfig

logger = logging.getLogger(__name__)

MODULE = "system"
ACTION = "update"

# 审计 SEC-07：base_url 由管理员可控，服务器会向该地址发起 POST（AI 请求）。
# 完全禁止内网地址会让「远程 Ollama / LM Studio」这类内网部署失效，
# 因此保留私网与回环地址，只挡云元数据 / 链路本地等明确的 SSRF 高危目标。
# 0.0.0.0 在此是「被拉黑的 SSRF 目标字符串」，不是监听地址（bandit B104 误报）
_BLOCKED_HOSTS = {"metadata", "metadata.google.internal", "0.0.0.0"}  # nosec B104


def _validate_base_url(raw) -> str:
    """校验 API 地址，返回错误消息；通过返回空串。"""
    raw = (raw or "").strip()
    if not raw:
        return ""  # 留空 = 使用 provider 默认地址
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        return "API 地址必须以 http:// 或 https:// 开头"
    if not parts.hostname:
        return "API 地址缺少主机名"
    if parts.username or parts.password:
        return "API 地址不允许携带用户名/密码"
    host = parts.hostname.strip("[]").lower()
    if host in _BLOCKED_HOSTS:
        return "API 地址不能指向云元数据等保留主机"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return ""
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return "API 地址不能指向链路本地/保留地址"
    return ""


def _float(raw, default):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _positive_int(raw, default):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _apply_form(config, data):
    """把表单值写入 config 实例（不落库）；provider 非法时回退默认值。"""
    provider = (data.get("provider") or "").strip()
    if provider in dict(AiProviderConfig.Provider.choices):
        config.provider = provider
    elif not config.pk:
        config.provider = AiProviderConfig.Provider.MINIMAX

    config.base_url = (data.get("base_url") or "").strip()
    config.set_api_key(data.get("api_key") or "")   # 保存前加密
    config.model = (data.get("model") or "").strip()
    config.temperature = _float(data.get("temperature"), 0.3)
    config.max_tokens = _positive_int(data.get("max_tokens"), 800)
    config.timeout = _positive_int(data.get("timeout"), 30)
    config.is_enabled = data.get("is_enabled") == "on"
    return config


def _audit(request, action, summary):
    try:
        log_operation(request, "系统管理", action, "AI配置", summary)
    except Exception:
        logger.exception("AI 配置操作留痕写入失败")


def _render(request):
    # 始终给出实例：无已保存配置时用未保存的默认实例填充表单（含各字段默认值）
    config = AiProviderConfig.current() or AiProviderConfig()
    return render(request, "ai_api/ai_config.html", {
        "config": config,
        "providers": AiProviderConfig.Provider.choices,
        "default_base_urls": AiProviderConfig.DEFAULT_BASE_URL,
        "effective_base_url": config.effective_base_url(),
        "api_key_masked": config.api_key_masked,   # 掩码显示，模板用此字段
    })


@perm_required(MODULE, ACTION)
def ai_config_view(request):
    return _render(request)


@perm_required(MODULE, ACTION)
@require_POST
def ai_config_save(request):
    config = AiProviderConfig.current() or AiProviderConfig()
    err = _validate_base_url(request.POST.get("base_url"))
    if err:
        messages.error(request, err)
        return _render(request)
    _apply_form(config, request.POST)
    config.updated_by = request.user.get_username()
    config.save()
    _audit(
        request, "编辑",
        f"保存 AI 配置（{config.get_provider_display()} / "
        f"{config.model or '未指定模型'} / "
        f"{'已启用' if config.is_enabled else '未启用'}）",
    )
    return redirect("ai_config")


@perm_required(MODULE, ACTION)
@require_POST
def ai_config_reset(request):
    """重置为默认：清空配置（回到未启用状态）。"""
    AiProviderConfig.objects.all().delete()
    _audit(request, "删除", "重置 AI 配置（清空为默认未启用状态）")
    return redirect("ai_config")


@perm_required(MODULE, ACTION)
@require_POST
def ai_config_test(request):
    """连通性测试：优先用页面当前填写的参数（未保存的也能测）。"""
    err = _validate_base_url(request.POST.get("base_url"))
    if err:
        return JsonResponse({"ok": False, "message": err})
    config = AiProviderConfig.current() or AiProviderConfig()
    _apply_form(config, request.POST)
    ok, message = test_connection(config)
    return JsonResponse({"ok": ok, "message": message})
