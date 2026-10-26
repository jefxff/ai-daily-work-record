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

from django.db.models import Count, F, Q, Value
from django.db.models.functions import Coalesce, NullIf, TruncDate, TruncHour
from django.shortcuts import render
from django.utils import timezone

from accounts.decorators import perm_required

from .audit_vocab import DELETE_ACTIONS, EXPORT_ACTIONS, UPDATE_ACTIONS
from .models import OperationLog

# 阈值集中定义，便于测试与后续调整
DELETE_BURST_THRESHOLD = 20      # 同一操作人 1 小时内删除条数上限
OFF_HOURS_BATCH_THRESHOLD = 10   # 同一操作人同一天（非工作时间）删除/编辑条数下限
HIGH_FREQ_THRESHOLD = 50         # 同一操作人 1 小时内操作总数上限
EXPORT_BURST_THRESHOLD = 10      # 同一操作人 1 小时内导出条数上限

DEFAULT_DAYS = 7
MIN_DAYS = 1
MAX_DAYS = 90


def _with_actor(qs):
    """按「账号优先、展示名兜底」附加聚合键 ``actor``。

    为什么不能直接按 ``operator`` 分组：那列是展示名，各模块口径不同（多数是
    用户名，档案/证照/试样是姓名），同一个人会被拆成两组，阈值判定失真
    （一致性审计 P0-2）。历史行没回填到账号时自动回退到展示名。
    """
    return qs.annotate(
        actor=Coalesce(NullIf(F("operator_username"), Value("")), F("operator"))
    )


def _clamp_days(raw):
    """解析 GET ``days``，非法值回退默认、越界夹取到 [MIN_DAYS, MAX_DAYS]。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return max(MIN_DAYS, min(MAX_DAYS, value))


def collect_anomalies(days_raw=None):
    """检测窗口内四类异常（页面与 API 共用的唯一口径）。"""
    days = _clamp_days(days_raw)
    since = timezone.now() - timedelta(days=days)
    base = OperationLog.objects.filter(operation_time__gte=since)

    # 1. 删除爆发：同一操作人 1 小时内删除条数 > 阈值
    delete_bursts = list(
        _with_actor(base.filter(operation_type__in=DELETE_ACTIONS))
        .annotate(hour=TruncHour("operation_time"))
        .values("actor", "hour")
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
        _with_actor(base.filter(operation_type__in=DELETE_ACTIONS + UPDATE_ACTIONS))
        .exclude(business_hours)
        .annotate(day=TruncDate("operation_time"))
        .values("actor", "day")
        .annotate(cnt=Count("id"))
        .filter(cnt__gte=OFF_HOURS_BATCH_THRESHOLD)
        .order_by("-cnt")
    )

    # 3. 单小时高频操作：同一操作人 1 小时内操作总数 > 阈值
    high_freq = list(
        _with_actor(base)
        .annotate(hour=TruncHour("operation_time"))
        .values("actor", "hour")
        .annotate(cnt=Count("id"))
        .filter(cnt__gt=HIGH_FREQ_THRESHOLD)
        .order_by("-cnt")
    )

    # 4. 导出爆发：同一操作人 1 小时内导出条数 > 阈值
    export_bursts = list(
        _with_actor(base.filter(operation_type__in=EXPORT_ACTIONS))
        .annotate(hour=TruncHour("operation_time"))
        .values("actor", "hour")
        .annotate(cnt=Count("id"))
        .filter(cnt__gt=EXPORT_BURST_THRESHOLD)
        .order_by("-cnt")
    )

    return {
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
    }


@perm_required("system", "read")
def audit_anomalies(request):
    """在指定窗口内统计删除爆发 / 非工作时间批量 / 单小时高频 / 导出爆发四类异常。"""
    return render(request, "records/audit_anomalies.html",
                  collect_anomalies(request.GET.get("days")))
