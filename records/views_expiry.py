"""到期预警：日历视图 + 本月到期清单导出。

跨模块页面（insurance + archive）：无现成的"任一模块"装饰器，故在视图内按来源
模块 read 权限过滤（见 records/expiry.py）；二者均无权限时渲染 403。
"""
from datetime import datetime, timedelta

from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from accounts.decorators import perm_required_any
from accounts.services import has_perm

from . import expiry
from .audit import log_operation

# GET 提前天数参数 -> 来源 kind
LEAD_PARAM_MAP = {
    "lead_insurance": "insurance",
    "lead_contract": "archive_contract",
    "lead_license": "license_expire",
    "lead_review": "license_review",
    "lead_borrow": "borrow_due",
}

STATUS_TEXT = {"expired": "已过期", "danger": "紧急", "warning": "临近", "success": "正常"}


def _clamp_int(raw, default, lo, hi):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def _has_any(user):
    return (has_perm(user, "insurance", "read")
            or has_perm(user, "archive", "read"))


def _status_key(item):
    return "expired" if item["days_left"] < 0 else item["severity"]


def _forbidden(request):
    return render(request, "accounts/403.html", {
        "required_module": "insurance/archive",
        "required_action": "read",
    }, status=403)


def _parse_date(raw):
    """解析 YYYY-MM-DD，非法值返回 None。"""
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _parse_month(raw):
    """解析 YYYY-MM，非法值返回 None。"""
    try:
        return datetime.strptime(raw, "%Y-%m").date().replace(day=1)
    except (TypeError, ValueError):
        return None


def _shift_month(month_start, delta):
    """月份偏移（delta=±1），返回偏移后的 1 号日期。"""
    year = month_start.year + (month_start.month - 1 + delta) // 12
    month = (month_start.month - 1 + delta) % 12 + 1
    return month_start.replace(year=year, month=month)


def _qs(request, **overrides):
    """以当前 GET 参数为基础生成查询串（overrides 中 None 表示删除该参数）。"""
    params = request.GET.copy()
    for key, value in overrides.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = value
    return params.urlencode()


@perm_required_any(("insurance", "read"), ("archive", "read"))
def expiry_calendar(request):
    """未来 N 天内到期事项，按月历浏览、点选日期过滤，红/黄/绿分级。"""
    if not _has_any(request.user):
        return _forbidden(request)

    days = _clamp_int(request.GET.get("days"), expiry.DEFAULT_HORIZON_DAYS, 30, 365)
    lead_overrides = {}
    for param, kind in LEAD_PARAM_MAP.items():
        raw = request.GET.get(param)
        if raw not in (None, ""):
            lead_overrides[kind] = _clamp_int(
                raw, expiry.DEFAULT_LEAD_DAYS[kind], 1, 180
            )

    items = expiry.collect_expiry_items(
        request.user, horizon_days=days, lead_overrides=lead_overrides,
    )
    today = timezone.localdate()
    lead_used = {**expiry.DEFAULT_LEAD_DAYS, **lead_overrides}

    # 月历：ym 指定展示月份（默认当月），钳制在数据窗口内，避免空月翻页
    win_start = today - timedelta(days=expiry.LOOKBACK_DAYS)
    win_end = today + timedelta(days=days)
    month_start = _parse_month(request.GET.get("ym")) or today.replace(day=1)
    month_start = max(win_start.replace(day=1), min(win_end.replace(day=1), month_start))
    prev_month = _shift_month(month_start, -1)
    next_month = _shift_month(month_start, 1)

    # 点选日期过滤：day 命中窗口才生效；点选后下方清单只列当日事项
    selected = _parse_date(request.GET.get("day"))
    if selected and not (win_start <= selected <= win_end):
        selected = None
    by_day = expiry.group_by_day(items)
    if selected:
        by_day = [g for g in by_day if g["date"] == selected]

    return render(request, "records/expiry_calendar.html", {
        "days": days,
        "today": today,
        "horizon_end": today + timedelta(days=days),
        "by_day": by_day,
        "summary": expiry.summarize(items),
        # 回填表单用（按 GET 参数名索引）
        "lead_form": {param: lead_used[kind] for param, kind in LEAD_PARAM_MAP.items()},
        # 供筛选 chips 展示生效值（按 kind 索引）
        "lead_used": lead_used,
        "kind_labels": {k: m["kind_label"] for k, m in expiry.SOURCE_META.items()},
        # 月历
        "month_start": month_start,
        "month_weeks": expiry.build_month_weeks(
            items, month_start.year, month_start.month, today=today, selected=selected,
        ),
        "month_has_prev": prev_month >= win_start.replace(day=1),
        "month_has_next": next_month <= win_end.replace(day=1),
        "qs_prev": _qs(request, ym=prev_month.strftime("%Y-%m"), day=None),
        "qs_next": _qs(request, ym=next_month.strftime("%Y-%m"), day=None),
        "qs_today_month": _qs(request, ym=None, day=None),
        # 点选某日的基础查询串（ym 固定为当前展示月，模板拼接 &day=YYYY-MM-DD）
        "qs_day_base": _qs(request, ym=month_start.strftime("%Y-%m"), day=None),
        "selected_day": selected,
        "qs_clear_day": _qs(request, day=None),
    })


@perm_required_any(("insurance", "read"), ("archive", "read"))
def expiry_export(request):
    """导出「本月到期清单」：当月到期 / 已过期的全部事项（仅用户可读来源）。"""
    if not _has_any(request.user):
        return _forbidden(request)

    today = timezone.localdate()
    items = expiry.collect_expiry_items(request.user)
    rows = expiry.month_items(items, today.year, today.month)
    if not rows:
        return HttpResponse("本月暂无到期事项")

    wb = Workbook()
    ws = wb.active
    ws.title = "本月到期清单"

    headers = ["序号", "类型", "名称", "说明", "到期日期", "剩余天数", "状态", "归属模块"]
    for col, header in enumerate(headers, 1):
        ws.cell(row=1, column=col, value=header)

    for serial, item in enumerate(rows, 1):
        ws.cell(row=serial + 1, column=1, value=serial)
        ws.cell(row=serial + 1, column=2, value=item["kind_label"])
        ws.cell(row=serial + 1, column=3, value=item["title"])
        ws.cell(row=serial + 1, column=4, value=item["subtitle"])
        ws.cell(row=serial + 1, column=5, value=item["date"].strftime("%Y-%m-%d"))
        ws.cell(row=serial + 1, column=6, value=item["days_left"])
        ws.cell(row=serial + 1, column=7, value=STATUS_TEXT[_status_key(item)])
        ws.cell(row=serial + 1, column=8, value=item["module"])

    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 20

    # 公式注入防护 + 中文文件名编码（与全站导出同一套工具）
    from work_record.export_utils import content_disposition, sanitize_xlsx_formulas

    sanitize_xlsx_formulas(wb)
    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    filename = f"本月到期清单-{today.strftime('%Y%m%d')}.xlsx"
    response["Content-Disposition"] = content_disposition(
        filename, ascii_fallback="expiry-this-month.xlsx"
    )
    wb.save(response)
    # 导出行为留痕：供审计异常检测识别批量导出
    log_operation(request, "到期预警", "导出", "[到期预警]下的导出",
                  f"导出本月到期清单: {len(rows)} 条")
    return response
