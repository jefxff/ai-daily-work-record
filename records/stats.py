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

from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce, Greatest
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


def _sum(qs, field) -> Decimal:
    """SUM 并保证空表返回 0（而不是 None）。"""
    return qs.aggregate(v=Coalesce(Sum(field), ZERO))["v"] or Decimal("0")


# ---------------- 各模块 ----------------

def decoration_stats() -> dict:
    qs = Decoration.objects.all()
    return {
        "total_decorations": qs.count(),
        "decoration_reduced_total": _sum(qs, "reduced_price"),
    }


def credit_stats() -> dict:
    return CustomerCredit.objects.aggregate(
        total=Count("id"),
        pending_site=Count("id", filter=Q(work_method="现场审核", result="待排期去现场")),
        agree=Count("id", filter=Q(result="同意")),
        disagree=Count("id", filter=Q(result="不同意")),
        distinct_customers=Count("customer_name", distinct=True),
    )


def customer_complaint_stats() -> dict:
    return CustomerComplaint.objects.aggregate(
        total=Count("id"),
        completed=Count("id", filter=Q(complaint_status="已完成")),
        # 与列表页"未完成"筛选一致：未完成 + 处理中
        incomplete=Count("id", filter=Q(complaint_status__in=["未完成", "处理中"])),
        pending=Count("id", filter=Q(complaint_status="未完成")),
    )


def lawsuit_stats() -> dict:
    return Complaint.objects.aggregate(
        total=Count("id"),
        completed=Count("id", filter=Q(progress="结案")),
        incomplete=Count("id", filter=~Q(progress="结案")),
    )


def price_stats() -> dict:
    """核价统计：核减金额下推为 SQL（MAX(三家报价) - 内控价）。

    与原 Python 版 `get_reduction_amount()` 口径逐条等价：
    - 内控价为空或 0 的行不计（原实现用 `if self.internal_control_price` 判真）；
    - 三家供应商报价全为空的行不计；
    - 空表返回 0 而非 None。
    """
    qs = PriceEvaluation.objects.all()
    max_price = Greatest(
        Coalesce(F("supplier1_price"), ZERO),
        Coalesce(F("supplier2_price"), ZERO),
        Coalesce(F("supplier3_price"), ZERO),
    )
    eligible = (
        qs.exclude(internal_control_price__isnull=True)
        .exclude(internal_control_price=0)
        .filter(
            Q(supplier1_price__isnull=False)
            | Q(supplier2_price__isnull=False)
            | Q(supplier3_price__isnull=False)
        )
    )
    total_reduction = eligible.aggregate(
        v=Coalesce(Sum(max_price - F("internal_control_price")), ZERO)
    )["v"] or Decimal("0")
    return {
        "total_price_evaluations": qs.count(),
        "total_price_reduction": total_reduction,
    }


def insurance_stats(today=None) -> dict:
    today = today or timezone.localdate()
    return Insurance.objects.aggregate(
        total=Count("id"),
        not_expired=Count("id", filter=Q(end_date__gt=today)),
    )


def daily_stats(user) -> dict:
    """每日记录按用户隔离：任何统计都只能基于本函数。"""
    return DailyRecord.objects.filter(user=user).aggregate(
        total=Count("id"),
        incomplete=Count("id", filter=Q(status="未完成")),
        completed=Count("id", filter=Q(status="完成")),
    )


def regulation_count() -> int:
    return Regulation.objects.count()


def metal_prices_today() -> list:
    """今日金属价格；当天未抓取时回退到最近一次抓取的整批数据。"""
    today = timezone.localdate()
    today_qs = MetalPrice.objects.filter(crawl_date=today)
    if today_qs.exists():
        return list(today_qs)
    last = MetalPrice.objects.order_by("-crawl_date", "-crawl_time").first()
    if not last:
        return []
    return list(MetalPrice.objects.filter(crawl_date=last.crawl_date))


def total_records(visible, base: int, user=None) -> int:
    """全站记录总数：口径统一在这里，漏模块的问题只会在这一处发生。

    base 为 records 各模块计数之和；visible 为用户可见模块集合，
    只在用户有权限时才把试样 / 档案 / 知识库的条数算进去（避免越权泄露规模）。
    知识库分支按可见性口径统计（公开 + 本人文档），user 缺省时退回公开口径。
    """
    total = base
    if "samples" in visible:
        from samples.models import Sample

        total += Sample.objects.filter(is_deleted=False).count()
    if "archive" in visible:
        from archive.models import Archive

        total += Archive.objects.count()
    if "knowledge" in visible:
        from django.db.models import Q

        from knowledge_base.models import KnowledgeDocument

        doc_q = Q(status="published")
        if user is not None and getattr(user, "is_authenticated", False):
            doc_q &= Q(visibility="public") | Q(owner=user)
        else:
            doc_q &= Q(visibility="public")
        total += KnowledgeDocument.objects.filter(doc_q).count()
    return total


# 数据看板 10 个模块卡（顺序即模板顺序；不含 metal_price / knowledge）
DASHBOARD_MODULE_KEYS = (
    "decoration", "customer_credit", "customer_complaint", "complaint",
    "regulation", "price_evaluation", "insurance", "daily_record",
    "samples", "archive",
)


def dashboard_summary(
    user, visible, *, insurance_total: int, insurance_active: int, insurance_expired: int
) -> dict:
    """看板顶部统计的唯一计算入口（复核报告 DATA-04）。

    改造前 dashboard() 自己又写了一遍 aggregate/sum，与 home() 的口径分叉。
    这里复用各模块 stats 函数，只在外层做组合；模板 context 键保持不变。
    保险数字由调用方按「标的聚合口径」传入，避免保单明细口径回潮。
    """
    dec = decoration_stats()
    credit = credit_stats()
    cc = customer_complaint_stats()
    law = lawsuit_stats()
    price = price_stats()
    daily = daily_stats(user)
    regulations = regulation_count()

    base = (
        dec["total_decorations"] + credit["distinct_customers"] + cc["total"]
        + law["total"] + regulations + price["total_price_evaluations"]
        + insurance_total + daily["total"]
    )
    total_processed = daily["completed"] + cc["completed"]
    total_items = daily["total"] + cc["total"]
    return {
        "total_records": total_records(visible, base, user=user),
        "module_count": sum(1 for key in DASHBOARD_MODULE_KEYS if key in visible),
        "completion_rate": int((total_processed / total_items) * 100) if total_items else 0,
        "pending_count": daily["incomplete"] + cc["pending"] + law["incomplete"],
        "total_decorations": dec["total_decorations"],
        "decoration_reduced_total": dec["decoration_reduced_total"],
        "total_customers": credit["distinct_customers"],
        "credit_total_count": credit["total"],
        "credit_pending_site_count": credit["pending_site"],
        "credit_agree_count": credit["agree"],
        "credit_disagree_count": credit["disagree"],
        "complaint_total_count": cc["total"],
        "complaint_completed_count": cc["completed"],
        "complaint_incomplete_count": cc["incomplete"],
        "complaint_pending_count": cc["pending"],
        "lawsuit_total_count": law["total"],
        "lawsuit_completed_count": law["completed"],
        "lawsuit_incomplete_count": law["incomplete"],
        "lawsuit_pending_count": law["incomplete"],
        "total_regulations": regulations,
        "price_total_count": price["total_price_evaluations"],
        "price_reduction_total": price["total_price_reduction"],
        "insurance_total_count": insurance_total,
        "insurance_not_expired_count": insurance_active,
        "insurance_expired_count": insurance_expired,
        "daily_total_count": daily["total"],
        "daily_incomplete_count": daily["incomplete"],
        "daily_pending_count": daily["incomplete"],
    }

