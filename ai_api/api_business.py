"""业务聚合 / 报表 / 档案表级导入导出的 DRF API（补齐 A 类页面缺口）。

口径约定（docs/api-conventions.md）：
- 权限：APIView 一律 ``permission_classes = [IsAuthenticated]`` + 视图内
  ``_require()``，并声明 ``perm_explicit_checks`` 供全站权限覆盖守卫识别；
- 响应：``{"ok": bool, "message": str, ...}``；
- 业务规则拒绝 422、参数问题 400、无权限 403（DRF 默认）；
- 数据口径不复制：周报/年度总结/异常检测/到期清单都直接复用页面层已抽出的
  `_weekly_payload` / `_annual_payload` / `collect_anomalies` / `records.expiry`；
- 导出统一 ``@guarded_export`` + ``max_export_rows()`` + ``content_disposition``
  + ``sanitize_xlsx_formulas``（后两项在 archive.import_export 内部已做）。
"""
import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.http import Http404
from django.utils import timezone
from django.utils.decorators import method_decorator

from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.services import has_perm
from archive.services.common import _log
from archive.services.import_export import (
    TABLE_SPECS, _apply_columns, _category_from_path, _finalize_cabinet,
    _get_spec, _match_obj, _read_import_rows, _export_response,
    _validate_archive_row,
)
from records import expiry
from records.models import (
    Complaint, CustomerComplaint, CustomerCredit, Decoration,
    Insurance, PriceEvaluation, Regulation,
)
from records.view_common import _my_daily_records
from records.views_audit import collect_anomalies
from records.views_reports import _annual_payload, _weekly_payload
from work_record.export_guard import (
    export_too_large_response, guarded_export, max_export_rows,
)

logger = logging.getLogger(__name__)


def _require(request, module, action):
    """视图内显式 RBAC 判定；未授权抛 403。"""
    if not has_perm(request.user, module, action):
        raise PermissionDenied("您没有执行该操作的权限。")


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _json_safe_row(row):
    """把 expiry 条目里的 date 转成字符串，保持其余字段原样。"""
    return {k: _iso(v) for k, v in row.items()}


class WeeklyReportAPIView(APIView):
    """GET /api/ai/daily-record/weekly-report/?start=YYYY-MM-DD（仅本人记录）。"""
    permission_classes = [IsAuthenticated]
    perm_explicit_checks = True

    def get(self, request):
        _require(request, "daily_record", "read")
        payload = _weekly_payload(request.user, request.query_params.get("start"))
        return Response({
            "ok": True,
            "message": "",
            "week_start": _iso(payload["week_start"]),
            "week_end": _iso(payload["week_end"]),
            "prev_start": _iso(payload["prev_start"]),
            "next_start": _iso(payload["next_start"]),
            "is_current_week": payload["is_current_week"],
            "stats": payload["stats"],
            "report_md": payload["report_md"],
            "ai_enabled": payload["ai_enabled"],
        })


class AnnualSummaryAPIView(APIView):
    """GET /api/ai/daily-record/annual-summary/?year=YYYY（仅本人记录）。"""
    permission_classes = [IsAuthenticated]
    perm_explicit_checks = True

    def get(self, request):
        _require(request, "daily_record", "read")
        payload = _annual_payload(request.user, request.query_params.get("year"))
        data = dict(payload)
        data["first_date"] = _iso(data["first_date"])
        data["last_date"] = _iso(data["last_date"])
        return Response({"ok": True, "message": "", **data})


class ExpiryCalendarAPIView(APIView):
    """GET /api/ai/expiry/calendar/?days=90&ym=YYYY-MM&day=YYYY-MM-DD

    跨模块（insurance / archive）：任一 read 即可，条目本身由
    records.expiry.collect_expiry_items 按用户权限过滤。
    """
    permission_classes = [IsAuthenticated]
    perm_explicit_checks = True

    def get(self, request):
        user = request.user
        if not (has_perm(user, "insurance", "read") or has_perm(user, "archive", "read")):
            raise PermissionDenied("您没有执行该操作的权限。")

        days = expiry.DEFAULT_HORIZON_DAYS
        raw_days = request.query_params.get("days")
        if raw_days:
            try:
                days = max(30, min(365, int(raw_days)))
            except (TypeError, ValueError):
                days = expiry.DEFAULT_HORIZON_DAYS

        items = expiry.collect_expiry_items(user, horizon_days=days)
        today = timezone.localdate()
        selected = request.query_params.get("day")
        groups = expiry.group_by_day(items)
        if selected:
            groups = [g for g in groups if g["date"].isoformat() == selected]

        return Response({
            "ok": True,
            "message": "",
            "days": days,
            "today": _iso(today),
            "horizon_end": _iso(today + timedelta(days=days)),
            "summary": expiry.summarize(items),
            "by_day": [
                {
                    "date": _iso(g["date"]),
                    "weekday": g["weekday"],
                    "items": [_json_safe_row(it) for it in g["items"]],
                }
                for g in groups
            ],
        })


class AuditAnomaliesAPIView(APIView):
    """GET /api/ai/operation-logs/anomalies/?days=7（四类异常检测，只读）。"""
    permission_classes = [IsAuthenticated]
    perm_explicit_checks = True

    def get(self, request):
        _require(request, "system", "read")
        payload = collect_anomalies(request.query_params.get("days"))
        return Response({"ok": True, "message": "", **payload})


class DataDownloadSummaryAPIView(APIView):
    """GET /api/ai/data-download/summary/：各模块可下载数据量（按权限裁剪）。"""
    permission_classes = [IsAuthenticated]
    perm_explicit_checks = True

    # (返回 key, 权限模块, 计数函数)
    MODULES = (
        ("decoration", "decoration", lambda: Decoration.objects.count()),
        ("customer_credit", "customer_credit", lambda: CustomerCredit.objects.count()),
        ("customer_complaint", "customer_complaint", lambda: CustomerComplaint.objects.count()),
        ("complaint", "complaint", lambda: Complaint.objects.count()),
        ("regulation", "regulation", lambda: Regulation.objects.count()),
        ("price_evaluation", "price_evaluation", lambda: PriceEvaluation.objects.count()),
        ("insurance", "insurance", lambda: Insurance.objects.count()),
    )

    def get(self, request):
        _require(request, "data_view", "read")
        counts = {}
        for key, module, counter in self.MODULES:
            if has_perm(request.user, module, "read"):
                counts[key] = counter()
        counts["daily_record"] = _my_daily_records(request.user).count()
        return Response({"ok": True, "message": "", "counts": counts})


class ArchiveTableExportAPIView(APIView):
    """GET /api/ai/archive/tables/<table>/export/：按表导出 xlsx（可回导）。"""
    permission_classes = [IsAuthenticated]
    perm_explicit_checks = True

    @method_decorator(guarded_export)
    def get(self, request, table):
        _require(request, "archive", "read")
        try:
            spec = _get_spec(table)
        except Http404:
            return Response({"ok": False, "message": "未知的导入导出对象"},
                            status=status.HTTP_404_NOT_FOUND)
        total = spec["queryset"]().count()
        limit = max_export_rows()
        if total > limit:
            return export_too_large_response(total, limit)
        _log(request, "导出", f"{spec['label']}导出", f"导出{spec['label']}清单")
        return _export_response(spec)


class ArchiveTableTemplateAPIView(APIView):
    """GET /api/ai/archive/tables/<table>/template/：下载表级导入模板。"""
    permission_classes = [IsAuthenticated]
    perm_explicit_checks = True

    def get(self, request, table):
        _require(request, "archive", "read")
        try:
            spec = _get_spec(table)
        except Http404:
            return Response({"ok": False, "message": "未知的导入导出对象"},
                            status=status.HTTP_404_NOT_FOUND)
        _log(request, "下载", f"{spec['label']}模板", f"下载{spec['label']}导入模板")
        return _export_response(spec, include_header_only=True)


class ArchiveTableImportAPIView(APIView):
    """POST /api/ai/archive/tables/<table>/import/：表级 upsert 导入。

    multipart: ``file`` 必填；``dry_run`` 默认 true（只预览不落库）。
    与页面端同一套匹配键 / 列容错 / 逐行事务规则。
    """
    permission_classes = [IsAuthenticated]
    perm_explicit_checks = True
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, table):
        _require(request, "archive", "update")
        try:
            spec = _get_spec(table)
        except Http404:
            return Response({"ok": False, "message": "未知的导入导出对象"},
                            status=status.HTTP_404_NOT_FOUND)

        upload = request.FILES.get("file")
        if not upload:
            return Response({"ok": False, "message": "请选择 Excel 文件（.xlsx / .xls / .csv）"},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            rows = _read_import_rows(upload)
        except Exception as exc:
            return Response({"ok": False, "message": f"文件解析失败：{exc}"},
                            status=status.HTTP_400_BAD_REQUEST)
        if not rows:
            return Response({"ok": False, "message": "未读取到有效数据"},
                            status=status.HTTP_400_BAD_REQUEST)
        if spec["match_header"] not in rows[0]:
            return Response({
                "ok": False,
                "message": f"缺少必需的匹配键列「{spec['match_header']}」",
            }, status=status.HTTP_400_BAD_REQUEST)

        raw_dry = str(request.data.get("dry_run", "true")).lower()
        dry_run = raw_dry not in ("0", "false", "no")

        is_archive_spec = spec is TABLE_SPECS["archives"]
        created_n = updated_n = 0
        errors, skipped, preview = [], [], []

        for idx, row in enumerate(rows, start=2):
            key_raw = str(row.get(spec["match_header"], "") or "").strip()
            # 预览：新增/更新判定（只读查询，不落库）
            exists = bool(key_raw) and _match_obj(spec, key_raw) is not None
            if spec["match_field"] == "path":
                exists = _category_from_path(key_raw) is not None
            if dry_run:
                if len(preview) < 20:
                    preview.append({"line": idx, "key": key_raw or "（空）",
                                    "action": "更新" if exists else "新增"})
                if is_archive_spec:
                    for col, reason in _validate_archive_row(row):
                        skipped.append(f"第{idx}行[{key_raw or '空'}] {col}：{reason}")
                created_n += 0 if exists else 1
                updated_n += 1 if exists else 0
                continue

            try:
                with transaction.atomic():
                    if spec["match_field"] == "path":
                        obj = _category_from_path(key_raw)
                        if obj is None:
                            raise ValueError("类别全路径为空")
                        created = obj._state.adding
                    else:
                        obj = _match_obj(spec, key_raw)
                        created = obj is None
                        if created:
                            if not key_raw:
                                raise ValueError(
                                    f"匹配键「{spec['match_header']}」为空，无法定位或新建")
                            if spec["match_field"] == "id":
                                raise ValueError(f"记录ID「{key_raw}」不存在")
                            obj = spec["model"](**{spec["match_field"]: key_raw[:100]})
                    field_errors = [] if is_archive_spec else None
                    _apply_columns(spec, obj, row, is_created=created,
                                   field_errors=field_errors)
                    if hasattr(obj, "refresh_status"):
                        obj.refresh_status()
                    obj.save()
                    _finalize_cabinet(obj)
                    if created:
                        created_n += 1
                    else:
                        updated_n += 1
                    for col, reason in (field_errors or []):
                        skipped.append(f"第{idx}行[{key_raw or '空'}] {col}：{reason}")
            except IntegrityError:
                errors.append(f"第{idx}行[{key_raw or '空'}]：存放位置已被其他档案占用，已跳过")
            except Exception as exc:
                errors.append(f"第{idx}行[{key_raw or '空'}]：{exc}")

        if not dry_run:
            _log(request, "导入", f"{spec['label']}导入",
                 f"新增 {created_n} 条，更新 {updated_n} 条，忽略单元格 "
                 f"{len(skipped)}，失败 {len(errors)} 条")

        return Response({
            "ok": not errors,
            "message": ("预览完成（未落库）" if dry_run
                        else f"导入完成：新增 {created_n} 条，更新 {updated_n} 条"),
            "dry_run": dry_run,
            "created": created_n,
            "updated": updated_n,
            "total": len(rows),
            "skipped": skipped,
            "errors": errors,
            "preview": preview,
        })
