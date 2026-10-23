"""records 各视图模块共用的轻量工具（拆分 views.py 时抽出，行为零变化）。"""

from datetime import timedelta

from django.db.models import Count
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.http import JsonResponse
from django.utils import timezone

from work_record.http_utils import is_ajax

from .models import DailyRecord

# 兼容旧调用名（各视图模块与测试仍在用 _is_ajax）
_is_ajax = is_ajax


def _my_daily_records(user):
    """每日记录数据隔离唯一起点：任何查询必须基于本函数，禁止裸 objects.all()。"""
    return DailyRecord.objects.filter(user=user)


def _get_my_record(request, pk):
    """行级归属校验：非本人记录一律 404（不返回 403，避免泄露记录存在性）。"""
    return get_object_or_404(DailyRecord, pk=pk, user=request.user)


def _modal_detail(request, template_name, context):
    """详情 AJAX 弹窗：返回局部模板 HTML"""
    html = render_to_string(template_name, context, request=request)
    return JsonResponse({'ok': True, 'html': html})


def _weekly_trend(user):
    """近 7 天每日记录数趋势（含今天，升序，缺数的天补 0，共 7 项）。

    单次按天聚合查询代替逐日 __date COUNT（原实现每页 7 条 SQL 的 N+1）。
    TruncDate 与 __date 查找一致按当前时区取日，结果等价。
    """
    today = timezone.localdate()
    start = today - timedelta(days=6)
    rows = (
        _my_daily_records(user)
        .filter(record_date__date__gte=start, record_date__date__lte=today)
        .annotate(day=TruncDate('record_date'))
        .values('day')
        .annotate(c=Count('id'))
    )
    day_counts = {r['day']: r['c'] for r in rows}
    return [
        {'date': (today - timedelta(days=i)).strftime('%m-%d'),
         'c': day_counts.get(today - timedelta(days=i), 0)}
        for i in range(6, -1, -1)
    ]
