"""日志中心：运行日志查看 / 手动采集 / 导出。

- ``log_center``：只读页面（system.read），按级别 / 文件 / 关键字 / 时间窗检索，
  打开页面时做一次 60s 节流的懒采集。
- ``log_ingest_now``：POST 手动采集（system.update），供排障时立即拉取最新日志。
- ``log_export``：导出当前筛选结果（CSV，走导出闸门与公式注入防护）。
"""
import csv
import logging

from django.conf import settings
from django.core.cache import caches
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.decorators import perm_required
from records.audit import log_operation
from work_record.export_guard import (
    export_too_large_response, guarded_export, max_export_rows,
)
from work_record.export_utils import content_disposition
from work_record.http_utils import per_page

from . import logs_service
from .models import SystemLog

logger = logging.getLogger(__name__)

AUDIT_MODULE = "系统管理"
LEVEL_VALUES = {level for level, _label in SystemLog.LEVEL_CHOICES}
DAYS_CHOICES = [
    (1, "近 1 天"),
    (3, "近 3 天"),
    (7, "近 7 天"),
    (30, "近 30 天"),
    (0, "全部"),
]
INGEST_THROTTLE_KEY = "systemlog:ingest:throttle"


class LogFilterError(ValueError):
    """筛选参数非法（级别 / 文件来源）。"""


def source_choices():
    return list(SystemLog.objects.values_list("source", flat=True).distinct().order_by("source"))


def parse_filters(params, sources=None):
    """解析并校验筛选参数，返回 (level, source, search, days)。"""
    level = (params.get("level") or "").strip().upper()
    if level and level not in LEVEL_VALUES:
        raise LogFilterError("无效的日志级别。")
    source = (params.get("source") or "").strip()
    if source:
        known = source_choices() if sources is None else sources
        if source not in known:
            raise LogFilterError("无效的日志文件来源。")
    search = (params.get("search") or params.get("q") or "").strip()[:100]
    days = logs_service.clamp_days(params.get("days"))
    return level, source, search, days


def build_queryset(level="", source="", search="", days=logs_service.DEFAULT_DAYS):
    return logs_service.apply_filters(
        SystemLog.objects.all(), level=level, source=source, search=search, days=days
    )


def _maybe_ingest():
    """页面打开时懒采集：60s 节流，先占坑再执行，防止并发重复。"""
    if not getattr(settings, "LOG_INGEST_ENABLED", True):
        return
    cache = caches["fast"]
    if cache.get(INGEST_THROTTLE_KEY):
        return
    cache.set(INGEST_THROTTLE_KEY, 1,
              int(getattr(settings, "LOG_INGEST_THROTTLE_SECONDS", 60)))
    try:
        logs_service.ingest_logs()
    except Exception:  # 采集失败不能阻断页面展示
        logger.exception("日志懒采集失败")


@perm_required("system", "read")
def log_center(request):
    sources = source_choices()
    try:
        level, source, search, days = parse_filters(request.GET, sources)
    except LogFilterError as exc:
        return HttpResponseBadRequest(str(exc), content_type="text/plain; charset=utf-8")

    _maybe_ingest()
    qs = build_queryset(level, source, search, days)
    page = Paginator(qs, per_page(request, 10, choices=(10, 30, 50))).get_page(
        request.GET.get("page")
    )
    return render(request, "notifications/log_center.html", {
        "logs": page,
        "level": level,
        "source": source,
        "search_term": search,
        "days": days,
        "days_choices": DAYS_CHOICES,
        "level_choices": SystemLog.LEVEL_CHOICES,
        "sources": sources,
        "stats": logs_service.dashboard_stats(),
        "files": logs_service.describe_files(),
        "ingest_enabled": bool(getattr(settings, "LOG_INGEST_ENABLED", True)),
    })


@perm_required("system", "update")
@require_POST
def log_ingest_now(request):
    try:
        result = logs_service.ingest_logs()
    except Exception:
        logger.exception("日志手动采集失败")
        return JsonResponse({"ok": False, "message": "采集失败，请稍后重试。"}, status=500)
    caches["fast"].set(INGEST_THROTTLE_KEY, 1,
                       int(getattr(settings, "LOG_INGEST_THROTTLE_SECONDS", 60)))
    message = (
        f"采集完成：扫描 {result['files']} 个文件，新增 {result['ingested']} 条"
        f"（重复 {result['skipped']} 条）"
    )
    if result["trimmed"]:
        message += f"，清理 {result['trimmed']} 条过期记录"
    return JsonResponse({"ok": True, "message": message, **result})


def _csv_safe(value):
    """CSV 公式注入防护：以 = + - @ 开头的文本前置单引号。"""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


@perm_required("system", "read")
@guarded_export
def log_export(request):
    try:
        level, source, search, days = parse_filters(request.GET)
    except LogFilterError as exc:
        return HttpResponseBadRequest(str(exc), content_type="text/plain; charset=utf-8")

    qs = build_queryset(level, source, search, days).order_by("-logged_at", "-id")
    total = qs.count()
    limit = max_export_rows()
    if total > limit:
        return export_too_large_response(total, limit)

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(["时间", "级别", "来源", "文件", "内容"])
    for row in qs.iterator():
        writer.writerow([
            timezone.localtime(row.logged_at).strftime("%Y-%m-%d %H:%M:%S"),
            _csv_safe(row.level),
            _csv_safe(row.logger),
            _csv_safe(row.source),
            _csv_safe(row.message),
        ])

    stamp = timezone.localtime().strftime("%Y%m%d_%H%M")
    response["Content-Disposition"] = content_disposition(
        f"运行日志_{stamp}.csv", ascii_fallback="system_logs.csv"
    )
    try:
        log_operation(
            request, AUDIT_MODULE, "导出", "运行日志",
            f"导出 {total} 条（级别={level or '全部'}，文件={source or '全部'}，"
            f"时间窗={days or '全部'}天，关键字={search or '无'}）",
        )
    except Exception:
        logger.exception("日志导出留痕失败")
    return response


def serialize_log(row, full=False):
    """API 输出结构（full=True 时带完整消息，否则截断 500 字）。"""
    message = row.message if full else row.message[:500]
    return {
        "id": row.id,
        "logged_at": timezone.localtime(row.logged_at).strftime("%Y-%m-%d %H:%M:%S"),
        "level": row.level,
        "logger": row.logger,
        "source": row.source,
        "message": message,
    }
