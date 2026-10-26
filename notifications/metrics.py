"""请求性能指标采集（批次 C）。

职责：
- ``record()``：把一次请求的指标（路径/状态码/耗时/SQL 条数与耗时/最慢 SQL）入内存缓冲；
- ``flush()``：缓冲达到条数或时间阈值时批量落库（``RequestMetric``），并按保留天数裁剪旧数据；
- ``make_query_collector()``：``connection.execute_wrapper`` 用的逐条 SQL 计时器；
- ``percentile()``：按 nearest-rank 计算 P95 等分位，供「性能监控」页使用。

设计取舍：写入走「内存缓冲 + 阈值批量落库」，避免每个请求都同步写一行拖慢响应；
落库失败仅记日志并丢批（指标为审计辅助数据，绝不影响请求主流程）。
"""
import logging
import threading
import time
from datetime import timedelta
from math import ceil

from django.conf import settings
from django.db import OperationalError
from django.utils import timezone

logger = logging.getLogger(__name__)

# —— 可调参数（settings 缺省时用这些值）——
_FLUSH_SIZE = getattr(settings, "REQUEST_METRICS_FLUSH_SIZE", 50)
_FLUSH_INTERVAL = getattr(settings, "REQUEST_METRICS_FLUSH_INTERVAL", 5)
_CLEANUP_INTERVAL = getattr(settings, "REQUEST_METRICS_CLEANUP_INTERVAL", 3600)
_RETENTION_DAYS = getattr(settings, "REQUEST_METRICS_RETENTION_DAYS", 30)

_lock = threading.Lock()
_buffer = []
_last_flush = time.monotonic()
_last_cleanup = time.monotonic()


def is_enabled():
    """采集开关：settings.REQUEST_METRICS_ENABLED（默认开启）。

    每次动态读取 settings，便于测试用 ``override_settings`` 开关；
    关闭时中间件零开销跳过。
    """
    return getattr(settings, "REQUEST_METRICS_ENABLED", True)


def reset():
    """清空内存缓冲与计时（仅供测试 / 手工运维调用）。"""
    global _last_flush, _last_cleanup
    with _lock:
        _buffer.clear()
        _last_flush = time.monotonic()
        _last_cleanup = time.monotonic()


def should_skip(path):
    """噪声路径不采集：静态资源、媒体文件、favicon、探针自身、perf 自举。

    /system/perf/ 自身也写 RequestMetric，会形成自举递归放大：越频繁访问 perf，
    它自己的统计数据就越多、查询越慢，越容易在慢查询里看到自己。直接跳过。
    """
    if not path:
        return True
    for prefix in ("/static/", "/media/", "/favicon.ico", "/probe/", "/system/perf/"):
        if path == prefix.rstrip("/") or path.startswith(prefix):
            return True
    return False


def make_query_collector(state):
    """构造逐条 SQL 计时的 ``execute_wrapper``。

    ``state`` 为请求级可变字典：``db_count`` / ``db_time_ms`` / ``slow_query``。
    仅当单条 SQL 耗时超过 ``REQUEST_METRICS_SLOW_QUERY_MS`` 时记录其 SQL 前缀。
    """
    slow_threshold = getattr(settings, "REQUEST_METRICS_SLOW_QUERY_MS", 200)

    def wrapper(execute, sql, params, many, context):
        started = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            state["db_count"] += 1
            state["db_time_ms"] += duration_ms
            if duration_ms >= slow_threshold:
                # 只保留最慢的一条（含耗时前缀），避免长 SQL 撑爆字段
                snippet = f"[{duration_ms:.0f}ms] {str(sql)[:500]}"
                if not state.get("slow_query"):
                    state["slow_query"] = snippet
                else:
                    try:
                        prev = float(state["slow_query"][1:].split("ms]")[0])
                    except (ValueError, IndexError):
                        prev = 0.0
                    if duration_ms > prev:
                        state["slow_query"] = snippet

    return wrapper


def record(*, method, path, status_code, duration_ms, db_count=0, db_time_ms=0,
           slow_query="", user=""):
    """把一次请求指标写入内存缓冲，达阈值则触发落库。"""
    global _last_flush
    entry = {
        "method": (method or "")[:10],
        "path": (path or "")[:255],
        "status_code": int(status_code or 0),
        "duration_ms": max(int(duration_ms or 0), 0),
        "db_count": max(int(db_count or 0), 0),
        "db_time_ms": max(int(db_time_ms or 0), 0),
        "slow_query": (slow_query or "")[:1000],
        "user": (user or "")[:100],
    }
    with _lock:
        _buffer.append(entry)
        due = len(_buffer) >= _FLUSH_SIZE or (time.monotonic() - _last_flush) >= _FLUSH_INTERVAL
    if due:
        flush()


def flush():
    """把缓冲批量落库；随后按保留天数裁剪。失败仅记日志（丢批，不影响请求）。"""
    global _last_flush
    from .models import RequestMetric

    with _lock:
        if not _buffer:
            _last_flush = time.monotonic()
            return
        batch = _buffer[:]
        _buffer.clear()
        _last_flush = time.monotonic()
    try:
        RequestMetric.objects.bulk_create([RequestMetric(**row) for row in batch])
    except Exception:
        logger.exception("请求性能指标落库失败（丢弃 %d 条）", len(batch))
    _maybe_cleanup()


def _maybe_cleanup():
    """按时间间隔触发一次旧数据裁剪（保留最近 RETENTION_DAYS 天）。"""
    global _last_cleanup
    now = time.monotonic()
    if now - _last_cleanup < _CLEANUP_INTERVAL:
        return
    _last_cleanup = now
    try:
        from .models import RequestMetric

        cutoff = timezone.now() - timedelta(days=_RETENTION_DAYS)
        RequestMetric.objects.filter(created_at__lt=cutoff).delete()
    except OperationalError:
        logger.warning("请求指标裁剪跳过：数据库忙")
    except Exception:
        logger.exception("请求指标裁剪失败")


def percentile(values, pct):
    """nearest-rank 分位：返回 ``values`` 中第 ceil(pct/100 * n) 小的值。

    ``values`` 需为可排序序列（如已升序的耗时列表）；空序列返回 None。
    """
    data = sorted(values)
    n = len(data)
    if n == 0:
        return None
    rank = max(min(ceil(pct / 100.0 * n), n), 1)
    return data[rank - 1]
