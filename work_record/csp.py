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
import logging.handlers as _lh
import os
import re
import secrets

from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from accounts.decorators import login_exempt

logger = logging.getLogger(__name__)

# CSP 违规独立审计通道：JSON Lines 格式写入 csp_violations.log
_CSP_LOG_DIR = os.environ.get('DJANGO_CSP_LOG_DIR', os.path.join(settings.BASE_DIR, 'logs'))
os.makedirs(_CSP_LOG_DIR, exist_ok=True)
_csp_file_handler = _lh.RotatingFileHandler(
    os.path.join(_CSP_LOG_DIR, 'csp_violations.log'),
    maxBytes=5*1024*1024, backupCount=3, encoding='utf-8',
)
_csp_file_handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
logger.addHandler(_csp_file_handler)


_SCRIPT_TAG_RE = re.compile(r"<script(?![^>]*\bnonce=)", re.IGNORECASE)


def _log_safe(value, limit=200):
    """清洗来自未认证请求体的日志字段（审计 BE-09）。

    浏览器上报的 blocked-uri / document-uri 等完全由客户端控制，
    换行符可伪造日志行、超长值会刷爆日志文件——落日志前统一压平并截断。
    """
    if not isinstance(value, str):
        value = str(value)
    return value.replace("\r", " ").replace("\n", " ")[:limit]


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
            "script-src-attr 'unsafe-inline'",
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
        logger.info(
            "CSP violation blocked=%s directive=%s uri=%s",
            _log_safe(report.get("blocked-uri")),
            _log_safe(report.get("violated-directive")),
            _log_safe(report.get("document-uri")),
        )
    except Exception:
        logger.debug("CSP 上报体解析失败", exc_info=True)
    return HttpResponse(status=204)
