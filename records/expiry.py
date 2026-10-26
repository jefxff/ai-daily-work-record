"""到期风暴服务层：聚合四类到期事项（纯读，不建表）。

四类来源分散在两个业务模块，历史上是三套独立的到期概念：
- insurance        保险          records.models.Insurance.end_date
- archive_contract  档案合同      archive.models.Archive.expire_date
- license_expire    证照有效期    archive.models.License.expire_date
- license_review    证照年审      archive.models.License.next_review_date

本模块把它们归一为同构条目，供「到期预警」日历页与「本月到期清单」导出共用。
权限：逐来源按所属模块 read 权限过滤——只聚合当前用户有权读取的来源。
"""
from datetime import timedelta

from django.utils import timezone

from accounts.services import has_perm

# 来源元数据：kind -> 所属模块 / 中文类型名 / 默认提前提醒天数
SOURCE_META = {
    "insurance": {"module": "insurance", "kind_label": "保险", "lead_days": 30},
    "archive_contract": {"module": "archive", "kind_label": "合同", "lead_days": 60},
    "license_expire": {"module": "archive", "kind_label": "证照有效期", "lead_days": 60},
    "license_review": {"module": "archive", "kind_label": "证照年审", "lead_days": 30},
    "borrow_due": {"module": "archive", "kind_label": "借阅应还", "lead_days": 30},
}

DEFAULT_LEAD_DAYS = {kind: meta["lead_days"] for kind, meta in SOURCE_META.items()}
DEFAULT_HORIZON_DAYS = 90   # 默认展望窗口（天）
LOOKBACK_DAYS = 90          # 已过期回溯窗口（天），避免历史噪声
URGENT_DAYS = 7             # 红/黄分界：≤7 天（或已过期）为红

WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _lead_map(overrides=None):
    """合并默认与覆盖值，得到 kind -> 提前天数。"""
    lead = dict(DEFAULT_LEAD_DAYS)
    for kind, value in (overrides or {}).items():
        if kind in lead and value:
            lead[kind] = int(value)
    return lead


def _severity(days_left, lead_days):
    """红/黄/绿分级：已过期或极近 → danger；提前期内 → warning；其余 → success。"""
    if days_left < 0 or days_left <= URGENT_DAYS:
        return "danger"
    if days_left <= lead_days:
        return "warning"
    return "success"


def _make_item(kind, title, subtitle, when, today, lead_days, link):
    days_left = (when - today).days
    return {
        "kind": kind,
        "kind_label": SOURCE_META[kind]["kind_label"],
        "title": title,
        "subtitle": subtitle,
        "date": when,
        "days_left": days_left,
        "severity": _severity(days_left, lead_days),
        "lead_days": lead_days,
        "module": SOURCE_META[kind]["module"],
        "link": link,
    }


def collect_expiry_items(user, *, horizon_days=DEFAULT_HORIZON_DAYS,
                         lead_overrides=None, today=None):
    """收集用户有权读取的、窗口内的全部到期事项（按 到期日, 类型 升序）。"""
    today = today or timezone.localdate()
    start = today - timedelta(days=LOOKBACK_DAYS)
    end = today + timedelta(days=horizon_days)
    lead = _lead_map(lead_overrides)

    items = []

    if has_perm(user, SOURCE_META["insurance"]["module"], "read"):
        from urllib.parse import quote

        from records import insurance_grouping
        from records.insurance_services import one_off_keys
        from records.models import Insurance

        # 与保险页同一口径：按「标的最新保单年度」聚合，而不是逐条保单流水。
        # 这样已续保的旧年度不会再以「已过期」出现在预警里；一次性标的不提醒。
        records = list(Insurance.objects.filter(end_date__gte=start))
        one_off = one_off_keys()
        for subject in insurance_grouping.build_subjects(records, today=today):
            if subject.key in one_off:
                continue
            expire_on = subject.latest.end
            if expire_on > end:
                continue
            latest_record = subject.latest.records[-1]
            subtitle = (latest_record.company_name or '').strip() or '、'.join(
                subject.latest.insurer_names
            )
            items.append(_make_item(
                "insurance", subject.label, subtitle, expire_on,
                today, lead["insurance"],
                f"/insurance/subjects/detail/?key={quote(subject.key)}",
            ))

    if has_perm(user, SOURCE_META["archive_contract"]["module"], "read"):
        from archive.models import Archive, BorrowRecord, License

        for obj in Archive.objects.filter(expire_date__range=(start, end)):
            items.append(_make_item(
                "archive_contract", obj.name, obj.code, obj.expire_date,
                today, lead["archive_contract"], f"/archive/archives/{obj.pk}/",
            ))
        for obj in License.objects.filter(expire_date__range=(start, end)):
            items.append(_make_item(
                "license_expire", obj.name, obj.code, obj.expire_date,
                today, lead["license_expire"], f"/archive/licenses/{obj.pk}/",
            ))
        for obj in License.objects.filter(next_review_date__range=(start, end)):
            items.append(_make_item(
                "license_review", obj.name, obj.code, obj.next_review_date,
                today, lead["license_review"], f"/archive/licenses/{obj.pk}/",
            ))
        # 未归还借阅的应还日期：days_left 由 due_time 实时计算，
        # 不依赖列表页的 status 刷新（逾期事项在此始终可见）
        for obj in BorrowRecord.objects.filter(
            return_time__isnull=True, due_time__range=(start, end),
        ).select_related("archive"):
            items.append(_make_item(
                "borrow_due", obj.archive.name,
                f"{obj.archive.code} · 借阅人 {obj.borrower}", obj.due_time,
                today, lead["borrow_due"], f"/archive/archives/{obj.archive_id}/",
            ))

    items.sort(key=lambda it: (it["date"], it["kind"]))
    return items


def group_by_day(items):
    """按到期日分组（升序），供「按天排列」渲染。"""
    buckets = {}
    for it in items:
        buckets.setdefault(it["date"], []).append(it)
    return [
        {"date": d, "weekday": WEEKDAY_LABELS[d.weekday()], "items": buckets[d]}
        for d in sorted(buckets)
    ]


def summarize(items):
    """统计各严重度数量（已过期单列）。"""
    summary = {"expired": 0, "urgent": 0, "warning": 0, "normal": 0, "total": len(items)}
    for it in items:
        if it["days_left"] < 0:
            summary["expired"] += 1
        elif it["severity"] == "danger":
            summary["urgent"] += 1
        elif it["severity"] == "warning":
            summary["warning"] += 1
        else:
            summary["normal"] += 1
    return summary


def month_items(items, year, month):
    """筛选到期日落在指定自然月内的条目（供「本月到期清单」导出）。"""
    return [it for it in items if it["date"].year == year and it["date"].month == month]


# ---- 月历网格（月视图浏览 + 点选日期过滤） ----

_SEVERITY_RANK = {"danger": 3, "warning": 2, "success": 1}


def _worst_key(item):
    """条目的最严重等级键：已过期视同 danger。"""
    return "danger" if item["days_left"] < 0 else item["severity"]


def month_day_map(items, year, month):
    """指定自然月内 日期 -> {count, worst, kinds}，供月历格子渲染。"""
    day_map = {}
    for it in month_items(items, year, month):
        cell = day_map.setdefault(it["date"], {"count": 0, "worst": "success", "kinds": set()})
        cell["count"] += 1
        cell["kinds"].add(it["kind"])
        if _SEVERITY_RANK[_worst_key(it)] > _SEVERITY_RANK[cell["worst"]]:
            cell["worst"] = _worst_key(it)
    return day_map


def build_month_weeks(items, year, month, *, today, selected=None):
    """把指定自然月排成 周 -> 日格 的二维结构（含前后月补位灰格）。

    每格：{date, in_month, count, worst, kinds(排序后列表), is_today, is_selected}；
    无事项的格子 count=0 / worst=None。
    """
    import calendar as _cal

    day_map = month_day_map(items, year, month)
    weeks = []
    for week in _cal.Calendar(firstweekday=0).monthdatescalendar(year, month):
        cells = []
        for d in week:
            info = day_map.get(d)
            cells.append({
                "date": d,
                "in_month": d.month == month,
                "count": info["count"] if info else 0,
                "worst": info["worst"] if info else None,
                "kinds": sorted(info["kinds"]) if info else [],
                "is_today": d == today,
                "is_selected": d == selected,
            })
        weeks.append(cells)
    return weeks
