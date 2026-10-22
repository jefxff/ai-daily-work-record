"""性能监控页 + 外部独立探针（批次 C）。

- ``perf_dashboard``：面向 ``system.read``，基于 RequestMetric 展示窗口内的
  请求量 / P95 延迟 / 慢请求 / 慢查询 Top，以及按端点的请求量与平均耗时榜单。
- ``probe``：免登录的轻量存活探针（不访问数据库），供外部监控独立轮询。
"""
import time
from datetime import timedelta
from math import ceil

from django.conf import settings
from django.db.models import Avg, Count, Max, Sum
from django.db.models.functions import TruncDate
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone

from accounts.decorators import login_exempt, perm_required

from .models import RequestMetric

DEFAULT_DAYS = 7
MIN_DAYS = 1
MAX_DAYS = 90
TOP_N = 10
RECENT_N = 20

_PROCESS_START = time.monotonic()


def _clamp_days(raw):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return max(MIN_DAYS, min(MAX_DAYS, value))


def _percentile_value(qs, field, pct):
    """用 SQL 定位法求分位：取升序第 ceil(pct/100 * n) 小的值（nearest-rank）。

    只取一行，避免把窗口内全部耗时读进内存。
    """
    total = qs.count()
    if not total:
        return None
    rank = max(min(ceil(pct / 100.0 * total), total), 1)
    return (
        qs.order_by(field)
        .values_list(field, flat=True)[rank - 1]
    )


@perm_required("system", "read")
def perf_dashboard(request):
    days = _clamp_days(request.GET.get("days"))
    since = timezone.now() - timedelta(days=days)
    qs = RequestMetric.objects.filter(created_at__gte=since)

    slow_ms = getattr(settings, "REQUEST_METRICS_SLOW_MS", 300)

    agg = qs.aggregate(
        avg_ms=Avg("duration_ms"),
        max_ms=Max("duration_ms"),
        db_time=Sum("db_time_ms"),
        db_count_total=Sum("db_count"),
    )
    total = qs.count()

    summary = {
        "total": total,
        "avg_ms": round(agg["avg_ms"] or 0),
        "p95_ms": _percentile_value(qs, "duration_ms", 95) or 0,
        "max_ms": agg["max_ms"] or 0,
        "slow_count": qs.filter(duration_ms__gt=slow_ms).count(),
        "error_count": qs.filter(status_code__gte=500).count(),
        "slow_query_count": qs.exclude(slow_query="").count(),
        "db_time_ms": agg["db_time"] or 0,
        "db_count_total": agg["db_count_total"] or 0,
    }

    top_paths = list(
        qs.values("path", "method")
        .annotate(cnt=Count("id"), avg_ms=Avg("duration_ms"), max_ms=Max("duration_ms"))
        .order_by("-cnt")[:TOP_N]
    )
    slow_paths = list(
        qs.values("path", "method")
        .annotate(cnt=Count("id"), avg_ms=Avg("duration_ms"), max_ms=Max("duration_ms"))
        .filter(cnt__gte=1)
        .order_by("-avg_ms")[:TOP_N]
    )
    recent_slow = list(
        qs.filter(duration_ms__gt=slow_ms).order_by("-created_at")[:RECENT_N]
    )
    recent_slow_queries = list(
        qs.exclude(slow_query="").order_by("-created_at")[:RECENT_N]
    )
    daily = list(
        qs.annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(cnt=Count("id"), avg_ms=Avg("duration_ms"))
        .order_by("day")
    )

    return render(request, "notifications/perf_dashboard.html", {
        "days": days,
        "since": since,
        "slow_ms": slow_ms,
        "summary": summary,
        "top_paths": top_paths,
        "slow_paths": slow_paths,
        "recent_slow": recent_slow,
        "recent_slow_queries": recent_slow_queries,
        "daily": daily,
        "probe_url": "/probe/",
    })


@login_exempt
def probe(request):
    """外部独立探针：不触碰数据库，仅证明 Web 进程存活。

    供内网/外部监控独立轮询（如 uptime 监控）。

    L9：匿名响应只返回 {"status": "ok"}——原先返回的 service 名 / 时间戳 /
    进程运行时长可被未授权者用于指纹识别与推断重启（补丁）周期；
    这类信息改由登录后、需 system.read 权限的 /system/health 完整版提供。
    """
    response = JsonResponse({"status": "ok"})
    response["Cache-Control"] = "no-store"
    return response
