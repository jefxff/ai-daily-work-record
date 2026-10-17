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
from django.http import FileResponse, Http404
from django.urls import path, re_path, include
from django.utils._os import safe_join
from django.contrib import admin
import mimetypes
from pathlib import Path


def protected_media(request, path):
    """受登录保护的媒体文件访问（URL 路径保持 /media/... 不变）。

    /media/ 已从全局登录白名单移除，未登录请求由 LoginRequiredMiddleware 拦截；
    已登录用户经此视图读取文件，safe_join 阻断 ../ 路径穿越。
    """
    try:
        full_path = Path(safe_join(settings.MEDIA_ROOT, path))
    except ValueError:
        raise Http404
    if not full_path.is_file():
        raise Http404
    content_type = mimetypes.guess_type(str(full_path))[0] or "application/octet-stream"
    return FileResponse(full_path.open("rb"), content_type=content_type)


urlpatterns = [
    path('admin/', admin.site.urls),
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
