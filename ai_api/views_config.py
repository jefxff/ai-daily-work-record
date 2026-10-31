"""AI 配置页面（系统管理，system.update 权限）。

配置按**用途**分开维护，页面顶部用标签页切换：

- ``purpose=polish``：周报 / 年度总结的润色（单例，行为与改造前一致）；
- ``purpose=chat``：AI 助手（Copilot）的对话问答。

两者必须能独立配置：Copilot 是"本地优先、云端兜底"，如果只能配润色，
用户就没有任何入口把问答切到本地模型，只能被动接受云端模型 + 敏感数据被拦。

- ai_config_view：GET 展示当前用途的配置与表单；
- ai_config_save：POST 保存并立即生效；
- ai_config_reset：POST 只重置当前用途（不连带删掉别的用途）；
- ai_config_test：POST 用提交或已存参数做一次连通性测试，返回 JSON 供页面 toast。

所有写操作均记录操作留痕；留痕失败仅记日志，不影响主流程。
"""

import ipaddress
import logging
from urllib.parse import urlsplit

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.decorators import perm_required
from records.audit import log_operation

from .client import test_connection
from .models import AiProviderConfig

logger = logging.getLogger(__name__)

MODULE = "system"
ACTION = "update"

# 编辑页支持的用途：值、标签、副标题、启用开关文案
PURPOSES = (
    {
        "value": AiProviderConfig.Purpose.POLISH,
        "label": "文本润色",
        "subtitle": "周报 / 年度总结的 AI 润色",
        "enable_label": "启用 AI 润色（勾选后生成周报 / 年度总结时自动调用）",
        "footer": "该配置仅影响「周报生成」与「年度总结」的润色；AI 不可用时会自动回退到模板文本，不影响功能可用性。",
    },
    {
        "value": AiProviderConfig.Purpose.CHAT,
        "label": "对话问答",
        "subtitle": "AI 助手（Copilot）的问答模型",
        "enable_label": "启用 AI 助手问答（勾选后 AI 助手用这个模型回答问题）",
        "footer": "AI 助手「本地优先、云端兜底」：勾选「本地模型」后视为数据不出内网，客诉 / 诉讼 / 保险等敏感模块才会对它开放；未勾选时这些模块会被外发策略拦截。",
    },
)
PURPOSE_MAP = {item["value"]: item for item in PURPOSES}
DEFAULT_PURPOSE = AiProviderConfig.Purpose.POLISH

# 某用途没有配置时，新建实例的初始形态。
# chat 默认「本地 Ollama + 不出内网」：问答默认就该走本地，用户想用云端再自己改，
# 而不是反过来——默认云端会让敏感模块一上来就查不到，还不知道为什么。
NEW_DEFAULTS = {
    AiProviderConfig.Purpose.CHAT: {
        "provider": AiProviderConfig.Provider.OLLAMA,
        "is_local": True,
        "temperature": 0.2,
        "max_tokens": 1200,
        "timeout": 60,
    },
}

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


def _apply_api_key(config, data):
    """处理密钥字段，返回错误消息（空串=通过）。

    必须区分「没改密钥」和「改了密钥」：配置页会把掩码回填进密码框，用户只改
    模型名时提交上来的就是掩码。早期实现直接加密落库，导致之后拿掩码当密钥去
    调模型，鉴权必然失败且页面上看不出异常（掩码显示依旧正常）。

    规则：
    - 勾选「清除已保存的密钥」→ 清空（本地 Ollama 等无需密钥的场景）；
    - 留空 → 保持已存密钥不变（密码框留空即「不修改」，与常见后台一致）；
    - 与当前掩码逐字相同 → 视为未修改（浏览器回填 / 旧页面缓存）；
    - 其它 → 作为新密钥加密保存；被判定为掩码则拒绝（由 set_api_key 抛错）。
    """
    raw = (data.get("api_key") or "").strip()
    if data.get("clear_api_key") == "on":
        config.set_api_key("")
        return ""
    if not raw:
        return ""
    if raw == config.api_key_masked:
        return ""
    try:
        config.set_api_key(raw)
    except ValueError as exc:
        return str(exc)
    return ""


def _purpose(request):
    """当前操作的用途。非法值一律回落到「文本润色」，不报错。"""
    raw = (request.POST.get("purpose") or request.GET.get("purpose") or "").strip()
    return raw if raw in PURPOSE_MAP else DEFAULT_PURPOSE


def _config_for(purpose):
    """取某用途的配置实例（未保存的新实例也返回，供表单回显）。

    严格按用途取，不做跨用途回退——回退会让「对话问答」页面上显示出润色那行，
    用户以为改了问答模型，实际改的是润色，Copilot 仍跑在云端模型上。
    """
    config = AiProviderConfig.exact_for(purpose)
    if config is not None:
        return config
    return AiProviderConfig(purpose=purpose, **NEW_DEFAULTS.get(purpose, {}))


def _apply_form(config, data, purpose):
    """把表单值写入 config 实例（不落库）；provider 非法时回退默认值。

    返回错误消息（空串=通过）。密钥字段走 ``_apply_api_key``，其余字段做
    类型兜底，保证任何输入都不会让配置页 500。
    """
    config.purpose = purpose

    provider = (data.get("provider") or "").strip()
    if provider in dict(AiProviderConfig.Provider.choices):
        config.provider = provider
    elif not config.pk:
        config.provider = AiProviderConfig.Provider.MINIMAX

    config.base_url = (data.get("base_url") or "").strip()
    key_error = _apply_api_key(config, data)   # 保存前加密；掩码会被拒绝
    config.model = (data.get("model") or "").strip()
    config.temperature = _float(data.get("temperature"), 0.3)
    config.max_tokens = _positive_int(data.get("max_tokens"), 800)
    config.timeout = _positive_int(data.get("timeout"), 30)
    config.is_enabled = data.get("is_enabled") == "on"
    # 数据分级放行的唯一依据：勾了才算"数据不出内网"，敏感模块才允许发送
    config.is_local = data.get("is_local") == "on"
    config.supports_tools = data.get("supports_tools") == "on"
    return key_error


def _audit(request, action, summary):
    try:
        log_operation(request, "系统管理", action, "AI配置", summary)
    except Exception:
        logger.exception("AI 配置操作留痕写入失败")


def _tab_status():
    """各用途的现状，供顶部标签页显示「已配置 / 未配置」。"""
    rows = []
    for item in PURPOSES:
        config = AiProviderConfig.exact_for(item["value"])
        rows.append({
            "value": item["value"],
            "label": item["label"],
            "configured": config is not None,
            "enabled": bool(config and config.is_enabled),
            "model": (config.model if config else "") or "",
            "is_local": bool(config and config.is_local),
        })
    return rows


def _chat_effective():
    """AI 助手**实际**会用的配置（含跨用途借用），用于页面上如实告知。

    ``borrowed`` 为真表示「对话问答」还没有自己的配置，Copilot 临时借用了
    别的用途（通常是润色）那一行——这正是"明明配了本地模型，徽章还显示云端"
    的根因之一，必须显式告诉用户。
    """
    from copilot.providers import router

    rows = router.candidates(AiProviderConfig.Purpose.CHAT)
    if not rows:
        return None
    first = rows[0]
    return {
        "id": first.pk,
        "provider": first.get_provider_display(),
        "model": first.model or "未指定模型",
        "is_local": first.is_local,
        "borrowed": first.purpose != AiProviderConfig.Purpose.CHAT,
        "purpose_label": PURPOSE_MAP.get(first.purpose, {}).get("label", first.purpose),
    }


def _render(request, purpose=None):
    purpose = purpose or _purpose(request)
    meta = PURPOSE_MAP[purpose]
    # 始终给出实例：无已保存配置时用该用途的默认实例填充表单（含各字段默认值）
    config = _config_for(purpose)
    return render(request, "ai_api/ai_config.html", {
        "config": config,
        "purpose": purpose,
        "purpose_label": meta["label"],
        "purpose_subtitle": meta["subtitle"],
        "purpose_enable_label": meta["enable_label"],
        "purpose_footer": meta["footer"],
        "tabs": _tab_status(),
        "chat_effective": _chat_effective(),
        "providers": AiProviderConfig.Provider.choices,
        "default_base_urls": AiProviderConfig.DEFAULT_BASE_URL,
        "effective_base_url": config.effective_base_url(),
        "api_key_masked": config.api_key_masked,   # 掩码显示，模板用此字段
        # 已存密钥本身是掩码=历史脏数据，页面必须提示重新录入，否则调用一直失败
        "api_key_is_masked": config.api_key_is_masked,
    })


@perm_required(MODULE, ACTION)
def ai_config_view(request):
    return _render(request)


@perm_required(MODULE, ACTION)
@require_POST
def ai_config_save(request):
    purpose = _purpose(request)
    config = _config_for(purpose)
    err = _validate_base_url(request.POST.get("base_url"))
    if err:
        messages.error(request, err)
        return _render(request, purpose)
    err = _apply_form(config, request.POST, purpose)
    if err:
        messages.error(request, f"未保存：{err}")
        return _render(request, purpose)
    config.updated_by = request.user.get_username()
    config.save()
    _audit(
        request, "编辑",
        f"保存 AI 配置（{PURPOSE_MAP[purpose]['label']} / "
        f"{config.get_provider_display()} / "
        f"{config.model or '未指定模型'} / "
        f"{'本地' if config.is_local else '云端'} / "
        f"{'已启用' if config.is_enabled else '未启用'}）",
    )
    return redirect(f"{reverse('ai_config')}?purpose={purpose}")


@perm_required(MODULE, ACTION)
@require_POST
def ai_config_reset(request):
    """重置当前用途：清空该用途的配置（回到未启用状态）。

    只删当前用途的行：配置按用途拆表后，全表 delete 会连带删掉另一种用途的
    配置，表现为「重置润色后 AI 助手也不能用了」。
    """
    purpose = _purpose(request)
    label = PURPOSE_MAP[purpose]["label"]
    deleted, _ = AiProviderConfig.objects.filter(purpose=purpose).delete()
    _audit(request, "删除", f"重置 AI 配置（{label}，清空 {deleted} 条，回到未启用状态）")
    return redirect(f"{reverse('ai_config')}?purpose={purpose}")


@perm_required(MODULE, ACTION)
@require_POST
def ai_config_test(request):
    """连通性测试：优先用页面当前填写的参数（未保存的也能测）。

    密钥留空时沿用已保存的密钥，因此「只改模型名后点测试」测的是真实密钥，
    而不是页面上显示的掩码。
    """
    purpose = _purpose(request)
    err = _validate_base_url(request.POST.get("base_url"))
    if err:
        return JsonResponse({"ok": False, "message": err})
    config = _config_for(purpose)
    err = _apply_form(config, request.POST, purpose)
    if err:
        return JsonResponse({"ok": False, "message": err})
    ok, message = test_connection(config)
    return JsonResponse({"ok": ok, "message": message})
