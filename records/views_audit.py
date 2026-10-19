"""审计异常行为检测（纯读取层）。

基于现有 ``records.models.OperationLog`` 做只读异常检测：

- **无新表、无 schema 变更**：不新增/修改任何 model 字段，无迁移；
- **无写入**：视图不产生 OperationLog，也不回写任何业务数据；
- **全 ORM 聚合**：三项检测都以 ``values(...).annotate(Count(...))`` 在数据库侧
  完成分组统计，不做 Python 层全表扫描。

检测项（窗口由 GET ``days`` 控制，默认 7 天，clamp 1–90）：

1. **删除爆发**：同一操作人 1 小时内删除条数 > ``DELETE_BURST_THRESHOLD``；
2. **非工作时间批量操作**：同一操作人同一天（工作时段外）删除/编辑条数
   >= ``OFF_HOURS_BATCH_THRESHOLD``；
3. **单小时高频操作**：同一操作人 1 小时内操作总数 > ``HIGH_FREQ_THRESHOLD``。

「导出/下载行为专项追踪」：批 B 已补齐各导出入口的 ``operation_type='导出'``
写点（records 标准导出 / 到期清单 / archive 表格与台账 / samples），本页据此
新增第 4 项检测（同一操作人 1 小时内导出条数超阈）。
"""
from datetime import timedelta

from django.db.models import Count, Q
from django.db.models.functions import TruncDate, TruncHour
from django.shortcuts import render
from django.utils import timezone

from accounts.decorators import perm_required

from .models import OperationLog

# 阈值集中定义，便于测试与后续调整
DELETE_BURST_THRESHOLD = 20      # 同一操作人 1 小时内删除条数上限
OFF_HOURS_BATCH_THRESHOLD = 10   # 同一操作人同一天（非工作时间）删除/编辑条数下限
HIGH_FREQ_THRESHOLD = 50         # 同一操作人 1 小时内操作总数上限
EXPORT_BURST_THRESHOLD = 10      # 同一操作人 1 小时内导出条数上限

DEFAULT_DAYS = 7
MIN_DAYS = 1
MAX_DAYS = 90


def _clamp_days(raw):
    """解析 GET ``days``，非法值回退默认、越界夹取到 [MIN_DAYS, MAX_DAYS]。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return max(MIN_DAYS, min(MAX_DAYS, value))


@perm_required("system", "read")
def audit_anomalies(request):
    """在指定窗口内统计删除爆发 / 非工作时间批量 / 单小时高频三类异常。"""
    days = _clamp_days(request.GET.get("days"))
    since = timezone.now() - timedelta(days=days)
    base = OperationLog.objects.filter(operation_time__gte=since)

    # 1. 删除爆发：同一操作人 1 小时内删除条数 > 阈值
    delete_bursts = list(
        base.filter(operation_type="删除")
        .annotate(hour=TruncHour("operation_time"))
        .values("operator", "hour")
        .annotate(cnt=Count("id"))
        .filter(cnt__gt=DELETE_BURST_THRESHOLD)
        .order_by("-cnt")
    )

    # 2. 非工作时间批量操作：排除工作时段（周一至周五 08:00–18:00）后按 (操作人, 日期) 分组
    business_hours = (
        Q(operation_time__hour__gte=8)
        & Q(operation_time__hour__lt=18)
        & Q(operation_time__week_day__in=[2, 3, 4, 5, 6])
    )
    off_hours = list(
        base.filter(operation_type__in=["删除", "编辑"])
        .exclude(business_hours)
        .annotate(day=TruncDate("operation_time"))
        .values("operator", "day")
        .annotate(cnt=Count("id"))
        .filter(cnt__gte=OFF_HOURS_BATCH_THRESHOLD)
        .order_by("-cnt")
    )

    # 3. 单小时高频操作：同一操作人 1 小时内操作总数 > 阈值
    high_freq = list(
        base.annotate(hour=TruncHour("operation_time"))
        .values("operator", "hour")
        .annotate(cnt=Count("id"))
        .filter(cnt__gt=HIGH_FREQ_THRESHOLD)
        .order_by("-cnt")
    )

    # 4. 导出爆发：同一操作人 1 小时内导出条数 > 阈值
    export_bursts = list(
        base.filter(operation_type="导出")
        .annotate(hour=TruncHour("operation_time"))
        .values("operator", "hour")
        .annotate(cnt=Count("id"))
        .filter(cnt__gt=EXPORT_BURST_THRESHOLD)
        .order_by("-cnt")
    )

    return render(request, "records/audit_anomalies.html", {
        "days": days,
        "since": since,
        "delete_bursts": delete_bursts,
        "off_hours": off_hours,
        "high_freq": high_freq,
        "export_bursts": export_bursts,
        "empty_all": not (delete_bursts or off_hours or high_freq or export_bursts),
        "delete_burst_threshold": DELETE_BURST_THRESHOLD,
        "off_hours_batch_threshold": OFF_HOURS_BATCH_THRESHOLD,
        "high_freq_threshold": HIGH_FREQ_THRESHOLD,
        "export_burst_threshold": EXPORT_BURST_THRESHOLD,
    })
