"""AI 用量与质量的量化采集。

只做**读**，不做任何写：这个模块同时被看板页、每日简报和按周筛选调用，
任何隐含写库都会让页面请求变脏。

口径（与计划书 §5 保持一致，避免各处各说一套）：
- **本地应答率** = 未调用模型（``total_tokens=0``）且成功的运行 ÷ 全部运行。
  判据用 token 而不是"走没走 planner"：本地链路的特征就是一个 token 都没花。
- **时延** = ``finished_at - created_at``，本地与模型分开算 P50/P95。
  混在一起算出来的 P95 既代表不了本地（太快），也代表不了模型（被拉低）。
- **不满意率** = 点踩 ÷ 有反馈的回答。沉默既不算满意，也不算不满意。
- **估算成本**：单价未知（默认未配置）时返回 ``None``，页面显示"—"。
  宁可留空，也不要编一个看起来好看的单价。
"""
import statistics
from collections import Counter, OrderedDict
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import (CopilotConversation, CopilotEgressLog, CopilotFeedback,
                     CopilotRun, CopilotToolCall)

PRICE_PER_1K_SETTING = 'COPILOT_PRICE_PER_1K'


def _pct(values, pct):
    """线性插值分位数；空列表返回 0。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * pct / 100
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return float(ordered[low] + (ordered[high] - ordered[low]) * (pos - low))


def _latency_seconds(runs):
    """完成耗时（秒）列表；没完成时间的（还在跑 / 异常中断）不参与统计。"""
    seconds = []
    for run in runs:
        if run.finished_at is None:
            continue
        delta = (run.finished_at - run.created_at).total_seconds()
        if delta >= 0:
            seconds.append(delta)
    return seconds


def _latency_summary(seconds):
    return {
        'count': len(seconds),
        'p50': round(_pct(seconds, 50), 3),
        'p95': round(_pct(seconds, 95), 3),
        'max': round(max(seconds), 3) if seconds else 0.0,
        'avg': round(statistics.fmean(seconds), 3) if seconds else 0.0,
    }


def _feedback_summary(since):
    rows = (CopilotFeedback.objects
            .filter(created_at__gte=since)
            .values_list('vote', flat=True))
    counter = Counter(rows)
    up, down = counter.get(1, 0), counter.get(-1, 0)
    rated = up + down
    return {
        'up': up, 'down': down, 'rated': rated,
        'down_rate': round(down / rated * 100, 1) if rated else 0.0,
    }


def _top_modules(runs, limit=6):
    """被问得最多的模块：按 ``run.steps`` 里的 module 聚合。"""
    from . import catalog

    counts = Counter()
    steps_list = (runs.exclude(status=CopilotRun.Status.PENDING)
                  .values_list('steps', flat=True))
    for steps in steps_list:
        for item in steps or []:
            key = item.get('module')
            if key:
                counts[key] += 1
    top = []
    for key, count in counts.most_common(limit):
        spec = catalog.MODULE_MAP.get(key)
        top.append({
            'module': key,
            'label': getattr(spec, 'label', key) or key,
            'calls': count,
        })
    return top


def _queue_depth():
    """线程池积压：SQLite 单写锁 + 2 个 worker，积压数比 CPU 更能解释"怎么这么慢"。"""
    try:
        from . import tasks
        queue = getattr(tasks.get_executor(), '_work_queue', None)
        return int(queue.qsize()) if queue is not None else 0
    except Exception:  # noqa: BLE001 - 拿不到队列显示 0，不影响其它指标
        return 0


def overview(days=30, now=None):
    """AI 质量与成本概览（默认近 30 天）。

    返回原始数值而不是格式化字符串：格式化是模板的事，掺进采集层会让
    测试只能比对字符串（"42.0%" 还是 "42%"？），回归成本立刻就上来了。
    """
    now = now or timezone.now()
    since = now - timedelta(days=max(1, int(days)))
    runs = CopilotRun.objects.filter(created_at__gte=since)

    total = runs.count()
    succeeded = runs.filter(status=CopilotRun.Status.SUCCEEDED).count()
    local_runs = list(runs.filter(
        Q(total_tokens=0) & Q(status=CopilotRun.Status.SUCCEEDED)))
    model_runs = list(runs.filter(total_tokens__gt=0))
    token_total = sum(r.total_tokens or 0 for r in runs)

    price = float(getattr(settings, PRICE_PER_1K_SETTING, 0) or 0)
    est_cost = round(token_total / 1000 * price, 4) if price else None

    error_counter = Counter(
        code for code, in runs.exclude(error_code='')
        .exclude(status=CopilotRun.Status.SUCCEEDED)
        .values_list('error_code')
    )
    errors = [{'code': code or '(空)', 'count': count}
              for code, count in error_counter.most_common(8)]

    tool_calls = CopilotToolCall.objects.filter(created_at__gte=since)
    tool_total = tool_calls.count()
    tool_denied = tool_calls.filter(permitted=False).count()

    egress = CopilotEgressLog.objects.filter(created_at__gte=since)
    conversations = CopilotConversation.objects.filter(created_at__gte=since)

    return OrderedDict([
        ('since', since),
        ('days', days),
        ('runs_total', total),
        ('runs_succeeded', succeeded),
        ('success_rate', round(succeeded / total * 100, 1) if total else 0.0),
        ('local_hits', len(local_runs)),
        ('model_calls', len(model_runs)),
        ('local_rate', round(len(local_runs) / total * 100, 1) if total else 0.0),
        ('latency_local', _latency_summary(_latency_seconds(local_runs))),
        ('latency_model', _latency_summary(_latency_seconds(model_runs))),
        ('token_total', token_total),
        ('token_avg_model', round(statistics.fmean(
            [r.total_tokens for r in model_runs]), 1) if model_runs else 0.0),
        ('price_per_1k', price or None),
        ('est_cost', est_cost),
        ('errors', errors),
        ('tool_total', tool_total),
        ('tool_denied', tool_denied),
        ('tool_denied_rate', round(tool_denied / tool_total * 100, 1)
         if tool_total else 0.0),
        ('top_modules', _top_modules(runs)),
        ('feedback', _feedback_summary(since)),
        ('conversations', conversations.count()),
        ('conversations_active', conversations.filter(
            status=CopilotConversation.Status.ACTIVE).count()),
        ('conversations_archived', conversations.filter(
            status=CopilotConversation.Status.ARCHIVED).count()),
        ('egress_total', egress.count()),
        ('egress_remote', egress.filter(is_local=False).count()),
        ('egress_remote_rows', sum(e.row_count or 0
                                   for e in egress.filter(is_local=False))),
        ('egress_blocked', error_counter.get('EGRESS_BLOCKED', 0)),
        ('queue_depth', _queue_depth()),
    ])
