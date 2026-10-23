"""统计看板口径（唯一实现）。

- 送样供应商数：周期内去重供应商总数（未指定供应商不计入）
- 样品总数：周期内新增样品主档数量（子记录不计入，天然不含重复轮次）
- 已通过样品数：默认以「已正式导入」为准，可切换为「至少一轮试样通过」
- 通过率 = 已通过样品数 ÷ 样品总数 × 100%
"""


from django.db.models import Count
from django.db.models.functions import TruncMonth
from django.utils import timezone

from .models import Sample, TestRecord


def preset_range(range_key):
    """快捷时间范围，返回 (date_from, date_to)。"""
    today = timezone.localdate()
    if range_key == "year":
        return today.replace(month=1, day=1), today
    if range_key == "quarter":
        first_month = (today.month - 1) // 3 * 3 + 1
        return today.replace(month=first_month, day=1), today
    if range_key == "all":
        earliest = (
            Sample.objects.filter(is_deleted=False)
            .order_by("sample_date")
            .values_list("sample_date", flat=True)
            .first()
        )
        return (earliest or today), today
    # 默认本月
    return today.replace(day=1), today


def _month_iter(start, end):
    """生成起止区间内每个月的 (year, month, 'YYYY-MM')。"""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m, f"{y:04d}-{m:02d}"
        m += 1
        if m == 13:
            y, m = y + 1, 1


def compute_dashboard(date_from, date_to, pass_basis="imported"):
    """聚合看板数据。pass_basis: imported=正式导入(默认) / test=试样通过。"""
    qs = Sample.objects.filter(
        is_deleted=False, sample_date__gte=date_from, sample_date__lte=date_to
    )

    total = qs.count()
    supplier_count = qs.exclude(supplier__isnull=True).values("supplier_id").distinct().count()

    if pass_basis == "test":
        passed = (
            TestRecord.objects.filter(
                sample__in=qs, review_conclusion=TestRecord.Conclusion.PASS
            )
            .values("sample_id")
            .distinct()
            .count()
        )
    else:
        passed = qs.filter(status=Sample.Status.IMPORTED).count()

    pass_rate = round(passed / total * 100, 1) if total else 0.0

    # 状态分布（保证 5 个状态键齐全）
    status_counts = {key: 0 for key, _ in Sample.Status.choices}
    for row in qs.values("status").annotate(c=Count("id")):
        status_counts[row["status"]] = row["c"]

    # 月度送样趋势（含空月补零）
    month_rows = {
        (row["month"].year, row["month"].month): row["c"]
        for row in qs.annotate(month=TruncMonth("sample_date"))
        .values("month")
        .annotate(c=Count("id"))
    }
    month_labels, month_values = [], []
    for y, m, label in _month_iter(date_from, date_to):
        month_labels.append(label)
        month_values.append(month_rows.get((y, m), 0))

    # 供应商分布 Top10
    supplier_rows = (
        qs.filter(supplier__isnull=False)
        .values("supplier__name")
        .annotate(c=Count("id"))
        .order_by("-c", "supplier__name")[:10]
    )
    supplier_labels = [row["supplier__name"] for row in supplier_rows]
    supplier_values = [row["c"] for row in supplier_rows]

    # 在途与终态汇总
    in_progress = (
        status_counts[Sample.Status.PENDING]
        + status_counts[Sample.Status.TESTING]
        + status_counts[Sample.Status.SMALL_BATCH]
    )
    terminated = status_counts[Sample.Status.TERMINATED]

    return {
        "total": total,
        "supplier_count": supplier_count,
        "passed": passed,
        "pass_rate": pass_rate,
        "pass_basis": pass_basis,
        "status_counts": status_counts,
        "in_progress": in_progress,
        "terminated": terminated,
        "month_labels": month_labels,
        "month_values": month_values,
        "supplier_labels": supplier_labels,
        "supplier_values": supplier_values,
    }
