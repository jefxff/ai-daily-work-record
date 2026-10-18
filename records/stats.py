"""records 业务统计口径（首页 home 与数据看板 dashboard 的唯一来源）。

存在的理由（审计 #21）：同一批数字在 home 与 dashboard 各写了一遍，口径很快漂移——
例如 dashboard 的"总记录数"只加了 8 个模块，漏掉试样 / 档案 / 知识库三个模块，
于是同一个系统里"总数"有两个答案，看板就没人信了。

约定：
- 所有函数只返回计数与金额，不含任何展示逻辑；
- 金额求和一律下推到数据库（Coalesce + Sum），不再用 Python 遍历求和；
- 仅"核减金额"因依赖三个报价字段取最大值，保留 Python 计算（已集中到本模块）。
"""

from decimal import Decimal

from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import (
    Complaint,
    CustomerComplaint,
    CustomerCredit,
    DailyRecord,
    Decoration,
    Insurance,
    MetalPrice,
    PriceEvaluation,
    Regulation,
)

ZERO = Value(Decimal("0"), output_field=DecimalField(max_digits=14, decimal_places=2))


def _sum(qs, field):
    """SUM 并保证空表返回 0（而不是 None）。"""
    return qs.aggregate(v=Coalesce(Sum(field), ZERO))["v"] or Decimal("0")


# ---------------- 各模块 ----------------

def decoration_stats():
    qs = Decoration.objects.all()
    return {
        "total_decorations": qs.count(),
        "decoration_reduced_total": _sum(qs, "reduced_price"),
    }


def credit_stats():
    return CustomerCredit.objects.aggregate(
        total=Count("id"),
        pending_site=Count("id", filter=Q(work_method="现场审核", result="待排期去现场")),
        agree=Count("id", filter=Q(result="同意")),
        disagree=Count("id", filter=Q(result="不同意")),
        distinct_customers=Count("customer_name", distinct=True),
    )


def customer_complaint_stats():
    return CustomerComplaint.objects.aggregate(
        total=Count("id"),
        completed=Count("id", filter=Q(complaint_status="已完成")),
        # 与列表页"未完成"筛选一致：未完成 + 处理中
        incomplete=Count("id", filter=Q(complaint_status__in=["未完成", "处理中"])),
        pending=Count("id", filter=Q(complaint_status="未完成")),
    )


def lawsuit_stats():
    return Complaint.objects.aggregate(
        total=Count("id"),
        completed=Count("id", filter=Q(progress="结案")),
        incomplete=Count("id", filter=~Q(progress="结案")),
    )


def price_stats():
    qs = PriceEvaluation.objects.all()
    # 核减金额 = 最高供应商报价 - 内控审核价；依赖三列取最大值，保留 Python 计算
    total_reduction = sum(
        (obj.get_reduction_amount() or 0) for obj in qs.only(
            "supplier1_price", "supplier2_price", "supplier3_price", "internal_control_price"
        )
    )
    return {
        "total_price_evaluations": qs.count(),
        "total_price_reduction": total_reduction,
    }


def insurance_stats(today=None):
    today = today or timezone.localdate()
    return Insurance.objects.aggregate(
        total=Count("id"),
        not_expired=Count("id", filter=Q(end_date__gt=today)),
    )


def daily_stats(user):
    """每日记录按用户隔离：任何统计都只能基于本函数。"""
    return DailyRecord.objects.filter(user=user).aggregate(
        total=Count("id"),
        incomplete=Count("id", filter=Q(status="未完成")),
        completed=Count("id", filter=Q(status="完成")),
    )


def regulation_count():
    return Regulation.objects.count()


def metal_prices_today():
    """今日金属价格；当天未抓取时回退到最近一次抓取的整批数据。"""
    today = timezone.localdate()
    today_qs = MetalPrice.objects.filter(crawl_date=today)
    if today_qs.exists():
        return list(today_qs)
    last = MetalPrice.objects.order_by("-crawl_date", "-crawl_time").first()
    if not last:
        return []
    return list(MetalPrice.objects.filter(crawl_date=last.crawl_date))


def total_records(visible, base):
    """全站记录总数：口径统一在这里，漏模块的问题只会在这一处发生。

    base 为 records 各模块计数之和；visible 为用户可见模块集合，
    只在用户有权限时才把试样 / 档案 / 知识库的条数算进去（避免越权泄露规模）。
    """
    total = base
    if "samples" in visible:
        from samples.models import Sample

        total += Sample.objects.filter(is_deleted=False).count()
    if "archive" in visible:
        from archive.models import Archive

        total += Archive.objects.count()
    if "knowledge" in visible:
        from knowledge_base.models import KnowledgeDocument

        total += KnowledgeDocument.objects.filter(status="published").count()
    return total
