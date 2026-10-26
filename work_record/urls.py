"""
URL configuration for work_record project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.shortcuts import render
from django.urls import path, re_path, include
from django.views.generic import RedirectView
from django.templatetags.static import static
from django.utils._os import safe_join
from django.contrib import admin
import mimetypes
from pathlib import Path

from accounts.services import has_perm
from accounts.views_token import obtain_api_token
from work_record.csp import csp_report
from work_record.export_utils import content_disposition
from ai_api import views_config as ai_config_views
from knowledge_base import views as kb_views
from notifications import views_health, views_tasks, views_metrics

# 媒体文件一级目录 -> 所属业务模块（与 accounts/registry.py 的模块 key 对齐）。
# 未登记的前缀一律拒绝：宁可让新目录"打不开"，也不能让它默认对所有登录用户开放。
MEDIA_PREFIX_MODULE = {
    "archives": "archive",
    "licenses": "archive",
    "proofs": "samples",
    "audits": "samples",
    "customer_ledgers": "customer_ledger",
}


def media_module_for(path: str):
    """返回路径所属模块 key；未登记返回 None。"""
    first = (path or "").replace("\\", "/").split("/", 1)[0]
    return MEDIA_PREFIX_MODULE.get(first)


def protected_media(request, path):
    """受保护的媒体文件访问（URL 路径保持 /media/... 不变）。

    三层校验：
    1. 登录（由 LoginRequiredMiddleware 完成，/media/ 不在白名单里）；
    2. 模块级授权：按路径一级目录判断用户是否有对应模块的 read 权限。
       改造前只判登录——任何登录用户改 URL 即可下载证照扫描件（审计 #5）；
    3. 路径穿越：safe_join 阻断 ../。
    响应补 X-Content-Type-Options: nosniff（审计 #26）。
    """
    module = media_module_for(path)
    if module is None:
        raise Http404
    if not has_perm(request.user, module, "read"):
        raise PermissionDenied("无权访问该文件")

    try:
        full_path = Path(safe_join(settings.MEDIA_ROOT, path))
    except ValueError:
        raise Http404
    if not full_path.is_file():
        raise Http404
    content_type = mimetypes.guess_type(str(full_path))[0] or "application/octet-stream"
    response = FileResponse(full_path.open("rb"), content_type=content_type)
    response["X-Content-Type-Options"] = "nosniff"

    # 仅图片与 PDF 允许浏览器内联渲染；其余（含 HTML/SVG/Office）一律强制下载，
    # 避免用户上传的 .html/.svg 在同源下被当作页面执行（存储型 XSS，审计 P1-2）。
    ext = full_path.suffix.lstrip(".").upper()
    if ext in {"PNG", "JPG", "JPEG", "GIF", "BMP", "WEBP", "PDF"}:
        ascii_name = f"preview.{ext.lower()}"
        response["Content-Disposition"] = content_disposition(
            full_path.name, ascii_fallback=ascii_name, inline=True
        )
        if ext == "PDF":
            # PDF 需允许同源 iframe 预览，同时用 CSP frame-ancestors 兜底（审计 P2-3）
            response["X-Frame-Options"] = "SAMEORIGIN"
            response["Content-Security-Policy"] = "frame-ancestors 'self'"
    else:
        suffix = full_path.suffix.lower()
        response["Content-Disposition"] = content_disposition(
            full_path.name, ascii_fallback=f"download{suffix}", inline=False
        )
    return response



urlpatterns = [
    path('csp-report/', csp_report, name='csp_report'),
    # 浏览器默认探测 /favicon.ico：重定向到静态图标，避免每会话一条 404（审计 FE-06）
    path('favicon.ico', RedirectView.as_view(url=static('img/favicon.ico'), permanent=False)),
    path('admin/', admin.site.urls),
    # 长期 Token 签发接口（供外部 Agent 免交互取用，视图内自行鉴权）
    path('api-token-auth/', obtain_api_token, name='api_token_auth'),
    # 外部独立存活探针（免登录、不访问数据库；供外部监控独立轮询）
    path('probe/', views_metrics.probe, name='probe'),
    path('accounts/', include('accounts.urls')),
    path('', include('records.urls')),
    path('api/', include('ai_api.urls')),
    path('knowledge/', include('knowledge_base.urls')),
    path('samples/', include('samples.urls')),
    path('archive/', include('archive.urls')),
    path('notifications/', include('notifications.urls')),
    path('system/backup/', include('sysbackup.urls')),
    # 系统健康面板（只读）：挂在 system/ 下，与「系统」侧栏组语义一致
    path('system/health/', views_health.system_health, name='system_health'),
    # 性能监控（请求量 / P95 / 慢查询，system.read 只读）
    path('system/perf/', views_metrics.perf_dashboard, name='perf_dashboard'),
    # 知识库阅读报告（system.read）：挂在 system/ 下，避免对普通知识库用户暴露他人阅读轨迹
    path('system/knowledge-report/', kb_views.knowledge_report, name='knowledge_report'),
    # 定时任务执行历史（system.read 只读）
    path('system/tasks/', views_tasks.task_run_history, name='task_run_history'),
    # AI 配置页（周报 / 年度总结的 AI 润色接入，system.update 权限）
    path('system/ai-config/', ai_config_views.ai_config_view, name='ai_config'),
    path('system/ai-config/save/', ai_config_views.ai_config_save, name='ai_config_save'),
    path('system/ai-config/reset/', ai_config_views.ai_config_reset, name='ai_config_reset'),
    path('system/ai-config/test/', ai_config_views.ai_config_test, name='ai_config_test'),
    # 用户上传文件：必须登录后访问（替代原先 DEBUG 下的 static() 匿名直出）
    re_path(r'^media/(?P<path>.*)$', protected_media),
]

if not settings.DEBUG:
    # DEBUG=0 时 runserver 不再自动服务 /static/。本项目为单进程内网部署，无
    # whitenoise / 前置代理；若不显式挂载，内网用户会看到丢失全部 CSS/JS 的页面。
    #
    # 审查 H4：原先长期使用 staticfiles 的**开发视图**（finders 全盘查找）当生产
    # 方案。现在改为：优先从 STATIC_ROOT（collectstatic 产物，语义明确、查找成本低、
    # 可用 immutable 缓存）返回；只有忘了 collectstatic 时才回退到 finders，
    # 保证「零配置也不丢样式」，同时把回退情况记进日志以便运维发现。
    import logging

    from django.contrib.staticfiles import finders
    from django.contrib.staticfiles.views import serve as staticfiles_serve  # noqa: F401
    from django.views.static import serve as _serve_static_file

    _static_logger = logging.getLogger(__name__)
    _static_root = Path(settings.STATIC_ROOT)

    def serve_static(request, path):
        """静态资源：STATIC_ROOT 优先，缺失时回退 finders（仅回退时告警一次/进程）。"""
        if (_static_root / path).is_file():
            return _serve_static_file(request, path, document_root=str(_static_root))
        if not getattr(serve_static, "_warned", False):
            _static_logger.warning(
                "STATIC_ROOT 未命中 %s，已回退到 staticfiles finders；"
                "建议执行 python manage.py collectstatic --noinput", path,
            )
            serve_static._warned = True
        return staticfiles_serve(request, path, insecure=True)

    urlpatterns += [
        re_path(r'^static/(?P<path>.*)$', serve_static),
    ]


# ---------- 自定义错误页 ----------
# 模板位于 templates/404.html / 500.html（项目级），handler 必须为模块级可调用对象。
# DEBUG=1 时 Django 默认渲染调试页（含完整 traceback），这是设计意图——DEBUG 下的
# handler404/500 行为可通过 DEBUG_PROPAGATE_EXCEPTIONS 调整，但为避免误伤调试场景，
# 这里 handler 始终指向项目级模板，DEBUG=1 下如需调试 traceback 可临时注释掉。

def error_404(request, exception):
    return render(request, '404.html', status=404)


def error_500(request):
    return render(request, '500.html', status=500)


handler404 = error_404
handler500 = error_500
