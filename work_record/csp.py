"""内容安全策略（CSP）中间件。

现状：全站仍有大量内联脚本与内联事件处理器，直接启用强制 CSP 会导致页面不可用。
因此默认使用 report-only：只上报违规、不拦截；同时始终下发不含 default-src 的
最小强制策略（object-src/base-uri/frame-ancestors/form-action），先拿到浏览器层
对点击劫持、<base> 注入、插件加载、跨站表单提交的缓解能力。

待内联脚本完成 nonce 化、内联事件处理器清理后，设置环境变量
DJANGO_CSP_MODE=enforce 即可把完整策略切换为强制模式，无需改代码。
"""

import json
import logging
import re
import secrets

from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from accounts.decorators import login_exempt

logger = logging.getLogger(__name__)

_SCRIPT_TAG_RE = re.compile(r"<script(?![^>]*\bnonce=)", re.IGNORECASE)


class CSPMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.mode = getattr(settings, "CSP_MODE", "report-only")

    def __call__(self, request):
        nonce = secrets.token_urlsafe(18)
        request.csp_nonce = nonce
        response = self.get_response(request)
        try:
            return self._apply(request, response, nonce)
        except Exception:  # CSP 失败绝不影响主请求
            logger.exception("CSP 响应处理失败")
            return response

    def _apply(self, request, response, nonce):
        # 与 CSP 模式解耦：任何响应都关闭本项目用不到的高权限 API
        response.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
            "magnetometer=(), gyroscope=(), accelerometer=(), fullscreen=(self)",
        )
        if self.mode == "off":
            return response
        # 媒体/下载响应可能已自行设置 frame-ancestors（PDF 预览），不覆盖
        if response.has_header("Content-Security-Policy"):
            return response
        content_type = response.get("Content-Type", "")
        if "text/html" not in content_type:
            return response

        if not response.streaming and response.content:
            html = response.content.decode("utf-8", errors="ignore")
            html = _SCRIPT_TAG_RE.sub(f'<script nonce="{nonce}"', html)
            response.content = html.encode("utf-8")
            if response.has_header("Content-Length"):
                del response["Content-Length"]

        report_policy = "; ".join([
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: blob:",
            "font-src 'self' data:",
            "connect-src 'self'",
            "frame-src 'self'",
            "media-src 'self'",
            "worker-src 'self'",
            "manifest-src 'self'",
            "report-uri /csp-report/",
        ])
        # 无论 report-only 还是 enforce，这四条都真正生效，且不会破坏内联脚本
        minimal_policy = (
            "object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'self'; form-action 'self'"
        )

        if self.mode == "enforce":
            response["Content-Security-Policy"] = f"{minimal_policy}; {report_policy}"
        else:
            response["Content-Security-Policy"] = minimal_policy
            response["Content-Security-Policy-Report-Only"] = report_policy
        return response


@csrf_exempt
@require_POST
@login_exempt
def csp_report(request):
    """接收浏览器 CSP 违规上报，仅落日志，不回传任何信息。"""
    if len(request.body or b"") > 16 * 1024:
        return HttpResponse(status=413)
    try:
        payload = json.loads(request.body.decode("utf-8", errors="ignore") or "{}")
        report = payload.get("csp-report") or {}
        logger.warning(
            "CSP violation blocked=%s directive=%s uri=%s",
            report.get("blocked-uri"), report.get("violated-directive"),
            report.get("document-uri"),
        )
    except Exception:
        logger.debug("CSP 上报体解析失败", exc_info=True)
    return HttpResponse(status=204)
