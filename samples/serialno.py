"""单据编号与轮次自动生成。

轮次/批次、单据编号全部由系统自动生成，人工仅核对不修改，
规避多轮次数据录入混乱、标注错误。
"""

from django.db.models import Max
from django.utils import timezone


def _next_serial(model, field, full_prefix):
    """按「前缀 + 流水」取下一个编号。

    流水按前缀后的完整剩余部分解析（超过 3 位自动扩位）；
    并通过存在性检查缓解局域网两人同时新建时 Max()+1 撞号。"""
    last = (
        model.objects.filter(**{f"{field}__startswith": full_prefix})
        .aggregate(m=Max(field))["m"]
    )
    seq = int(last[len(full_prefix):]) + 1 if last else 1
    candidate = f"{full_prefix}{seq:03d}"
    while model.objects.filter(**{field: candidate}).exists():
        seq += 1
        candidate = f"{full_prefix}{seq:03d}"
    return candidate


def next_daily_serial(model, field, prefix, date_obj=None):
    """生成「前缀 + YYYYMMDD + 3位流水」编号，如 S20260912001。"""
    day = date_obj or timezone.localdate()
    return _next_serial(model, field, f"{prefix}{day:%Y%m%d}")


def next_yearly_serial(model, field, prefix="P", year=None):
    """生成「前缀 + 年份 + 3位流水」档案编号，如 P2026001。

    与 next_daily_serial 统一为完整剩余解析，修复原「取末 3 位」
    在流水超过 999 时按 000 回卷导致编号冲突的问题。"""
    year = year or timezone.localdate().year
    return _next_serial(model, field, f"{prefix}{year}")


def next_round(model, sample):
    """同一样品内下一轮次/批次（从 1 开始），带存在性检查防并发重复。"""
    seq = (
        model.objects.filter(sample=sample).aggregate(m=Max("round_no"))["m"] or 0
    ) + 1
    while model.objects.filter(sample=sample, round_no=seq).exists():
        seq += 1
    return seq
