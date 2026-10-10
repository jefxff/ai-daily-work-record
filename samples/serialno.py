"""单据编号与轮次自动生成。

轮次/批次、单据编号全部由系统自动生成，人工仅核对不修改，
规避多轮次数据录入混乱、标注错误。
"""

from django.db.models import Max
from django.utils import timezone


def next_daily_serial(model, field, prefix, date_obj=None):
    """生成「前缀 + YYYYMMDD + 3位流水」编号，如 S20260912001。"""
    day = date_obj or timezone.localdate()
    full_prefix = f"{prefix}{day:%Y%m%d}"
    last = (
        model.objects.filter(**{f"{field}__startswith": full_prefix})
        .order_by()
        .aggregate(m=Max(field))["m"]
    )
    seq = int(last[len(full_prefix):]) + 1 if last else 1
    return f"{full_prefix}{seq:03d}"


def next_yearly_serial(model, field, prefix="P", year=None):
    """生成「前缀 + 年份 + 3位流水」档案编号，如 P2026001。"""
    year = year or timezone.localdate().year
    full_prefix = f"{prefix}{year}"
    last = (
        model.objects.filter(**{f"{field}__startswith": full_prefix})
        .order_by()
        .aggregate(m=Max(field))["m"]
    )
    seq = int(last[-3:]) + 1 if last else 1
    return f"{full_prefix}{seq:03d}"


def next_round(model, sample):
    """同一样品内下一轮次/批次（从 1 开始）。"""
    return (
        model.objects.filter(sample=sample).order_by().aggregate(m=Max("round_no"))["m"] or 0
    ) + 1
