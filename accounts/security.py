"""登录失败锁定：基于 Django cache，按 IP + 用户名计数。

5 次失败后锁定 15 分钟（阈值/时长见 settings.LOGIN_FAILURE_LOCK*）。
"""

from django.conf import settings
from django.core.cache import cache


def _cache_key(ip, username):
    return f"login-fail:{ip or 'unknown'}:{username.strip().lower()}"


def is_locked(ip, username):
    """返回 (是否锁定, 剩余秒数)。"""
    count = cache.get(_cache_key(ip, username), 0)
    if count >= getattr(settings, "LOGIN_FAILURE_LIMIT", 5):
        # 计数 key 带超时，剩余时间由 cache ttl 反映；无 ttl 接口时返回配置时长
        return True, getattr(settings, "LOGIN_LOCKOUT_SECONDS", 15 * 60)
    return False, 0


def record_failure(ip, username):
    """记录一次失败，返回当前失败次数。"""
    key = _cache_key(ip, username)
    count = cache.get(key, 0) + 1
    cache.set(key, count, getattr(settings, "LOGIN_LOCKOUT_SECONDS", 15 * 60))
    return count


def clear_failures(ip, username):
    cache.delete(_cache_key(ip, username))


def client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")
