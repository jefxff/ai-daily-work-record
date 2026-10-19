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
from django.urls import path, re_path, include
from django.utils._os import safe_join
from django.contrib import admin
import mimetypes
from pathlib import Path

from accounts.services import has_perm
from accounts.views_token import obtain_api_token

# 媒体文件一级目录 -> 所属业务模块（与 accounts/registry.py 的模块 key 对齐）。
# 未登记的前缀一律拒绝：宁可让新目录"打不开"，也不能让它默认对所有登录用户开放。
MEDIA_PREFIX_MODULE = {
    "archives": "archive",
    "licenses": "archive",
    "proofs": "samples",
    "audits": "samples",
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
    return response



urlpatterns = [
    path('admin/', admin.site.urls),
    # 长期 Token 签发接口（供外部 Agent 免交互取用，视图内自行鉴权）
    path('api-token-auth/', obtain_api_token, name='api_token_auth'),
    path('accounts/', include('accounts.urls')),
    path('', include('records.urls')),
    path('api/', include('ai_api.urls')),
    path('knowledge/', include('knowledge_base.urls')),
    path('samples/', include('samples.urls')),
    path('archive/', include('archive.urls')),
    path('notifications/', include('notifications.urls')),
    path('system/backup/', include('sysbackup.urls')),
    # 用户上传文件：必须登录后访问（替代原先 DEBUG 下的 static() 匿名直出）
    re_path(r'^media/(?P<path>.*)$', protected_media),
]
