"""静态资源缓存头中间件（审计 FE-06）。

项目未使用 WhiteNoise/Nginx，静态文件由 Django staticfiles 视图直接返回。
模板中的项目 CSS/JS 已带 `?v=` 手工版本号，命中版本号时下发一年 immutable，
未命中时保持短缓存，避免升级后旧缓存无法失效。
"""

from django.conf import settings


class StaticCacheMiddleware:
    IMMUTABLE_MAX_AGE = 31536000
    DEFAULT_MAX_AGE = 3600

    def __init__(self, get_response):
        self.get_response = get_response
        self.static_url = getattr(settings, "STATIC_URL", "/static/")

    def __call__(self, request):
        response = self.get_response(request)
        path = request.path or ""
        if not path.startswith(self.static_url):
            return response
        if response.status_code not in (200, 304):
            return response

        if "v=" in (request.META.get("QUERY_STRING") or ""):
            response["Cache-Control"] = (
                f"public, max-age={self.IMMUTABLE_MAX_AGE}, immutable"
            )
        else:
            response["Cache-Control"] = f"public, max-age={self.DEFAULT_MAX_AGE}"
        return response
