"""主动提醒的三条确定性规则（B3-2）。

为什么是"规则"而不是"让模型看着办"：
- 规则可复现。同一份数据明天跑还是这个结果，运维能在管理后台把它点出来；
  模型的判断今天对明天不一定对，误报还没法申诉。
- 规则不花钱。这类判断每天要为几十个账号各跑一遍。
- 规则可关。误报一次就该能一键屏蔽（`copilot_alerts --mute <rule>`），
  模型链路做不到"针对这一条规则不再提醒"。

模型在这件事里的位置只有一处：**润色措辞**（可选、默认关闭）。判断和
数字全部本地自治，模型连数据都看不到。
"""
from collections import Counter
from datetime import timedelta

NO_REPORT_MIN_WORKDAYS = 3     # 连续 N 个工作日没有日报才提醒（含当天）
COMPLAINT_WINDOW_DAYS = 30     # 同一客户在这个窗口内达到阈值才算"激增"
COMPLAINT_THRESHOLD = 3
OVERDUE_JUMP_DAYS = 7          # 逾期堆积：N 天内新增超过 2 项才提醒
OVERDUE_JUMP_THRESHOLD = 2


def _reverse_or(name, fallback):
    """站内路径尽量反查；反查不到就用兜底字面量。

    这些链接只在通知里出现，写死路径会因为一次路由改名悄悄失效——
    所以先反查。反查失败（比如该 url 不在当前 INSTALLED_APPS 里）也不能报错。
    """
    try:
        from django.urls import reverse

        return reverse(name)
    except Exception:  # noqa: BLE001
        return fallback


def _last_workdays(today, count):
    """最近 N 个工作日（周一~周五），含今天；周一到周日连续回溯。"""
    days, cursor = [], today
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return days


def rule_no_daily_report(user, today):
    """规则 1：连续 N 个工作日没有日报。

    只算工作日：周日晚上提醒"你今天没写日报"是最容易被判定为噪音的一类，
    而这类噪音一旦出现，用户会把**整类**提醒都关掉。
    """
    from accounts.services import has_perm
    from work_record.models import DailyRecord

    if not has_perm(user, 'daily_record', 'read'):
        return None
    days = _last_workdays(today, NO_REPORT_MIN_WORKDAYS)
    exists = DailyRecord.objects.filter(
        user=user, record_date__date__in=days).exists()
    if exists:
        return None
    return {
        'rule': 'no_daily_report',
        'title': f'已连续 {NO_REPORT_MIN_WORKDAYS} 个工作日没有日报',
        'body': '日报是周报与月度总结的唯一数据源，缺几天后面补起来很费劲。'
                '可以在列表页一次性补录，也可以直接问 AI 助手帮你列出来。',
        'link': _reverse_or('daily_record_list', '/daily_record/'),
    }


def rule_complaint_surge(user, today):
    """规则 2：同一客户近期客诉集中爆发（同一窗口内 ≥ 阈值）。"""
    from accounts.services import has_perm
    from records.models import CustomerComplaint

    if not has_perm(user, 'customer_complaint', 'read'):
        return None
    since = today - timedelta(days=COMPLAINT_WINDOW_DAYS)
    counter = Counter(
        CustomerComplaint.objects
        .filter(complaint_date__gte=since)
        .values_list('customer_name', flat=True)
    )
    if not counter:
        return None
    top_name, top_count = counter.most_common(1)[0]
    if top_count < COMPLAINT_THRESHOLD:
        return None
    unresolved = CustomerComplaint.objects.filter(
        customer_name=top_name, complaint_date__gte=since,
        complaint_status__in=['未完成', '处理中']).count()
    return {
        'rule': 'complaint_surge',
        'title': f'{top_name} 近 {COMPLAINT_WINDOW_DAYS} 天有 {top_count} 条客诉',
        'body': f'其中 {unresolved} 条仍未闭环。同一客户短期内反复出现，'
                '通常指向某一批次或同一工艺问题，建议先翻汇总再看单条明细。',
        'link': _reverse_or('customer_complaint_list', '/customer_complaint/'),
    }


def rule_overdue_pileup(user, today):
    """规则 3：逾期事项在堆积（比 N 天前新增超过阈值）。

    单条逾期本身已经有到期预警在管；这条规则看的是**趋势**——
    数量在涨说明没人处理，这才是需要被推到眼前的事。
    """
    from records import expiry

    current = [it for it in expiry.collect_expiry_items(
        user, horizon_days=1, today=today) if it['days_left'] < 0]
    before = [it for it in expiry.collect_expiry_items(
        user, horizon_days=1, today=today - timedelta(days=OVERDUE_JUMP_DAYS))
        if it['days_left'] < 0]
    added = len(current) - len(before)
    if len(current) == 0 or added < OVERDUE_JUMP_THRESHOLD:
        return None
    kinds = Counter(it['kind_label'] for it in current)
    top = '、'.join(f'{label} {count} 项' for label, count in kinds.most_common(3))
    return {
        'rule': 'overdue_pileup',
        'title': f'逾期事项比 {OVERDUE_JUMP_DAYS} 天前多了 {added} 项（共 {len(current)} 项）',
        'body': f'分布：{top}。逾期项越堆越多通常意味着责任人没定或流程卡住了，'
                '建议先从最早的那几项开始清。',
        'link': _reverse_or('expiry_calendar', '/expiry/calendar/'),
    }


RULES = {
    'no_daily_report': rule_no_daily_report,
    'complaint_surge': rule_complaint_surge,
    'overdue_pileup': rule_overdue_pileup,
}

TASK_CODE = 'copilot_alerts'
MUTE_PARAM = 'muted_rules'


def muted_rules():
    """被一键屏蔽的规则（写在 ScheduledTask.params 里）。

    放任务参数而不是用户偏好表：这是**运维/全员**层面的开关——
    "这条规则整体别再提醒了"，而不是"这个用户不想看"。
    """
    try:
        from notifications.models import ScheduledTask

        task = ScheduledTask.objects.filter(code=TASK_CODE).first()
        return set((task.params or {}).get(MUTE_PARAM) or []) if task else set()
    except Exception:  # noqa: BLE001 - 任务表不存在时不阻断提醒本身
        return set()


def set_muted(rule_code, muted=True):
    """屏蔽 / 恢复某条规则；返回 (是否为修改操作, 当前屏蔽集合)。"""
    from notifications.models import ScheduledTask

    if rule_code not in RULES:
        raise KeyError(rule_code)
    task, _ = ScheduledTask.objects.get_or_create(
        code=TASK_CODE,
        defaults={'name': 'AI 规则提醒', 'description': '命中业务异常规则时主动提醒',
                  'enabled': True, 'trigger_type': ScheduledTask.TriggerType.DAILY},
    )
    current = set(task.params.get(MUTE_PARAM) or [])
    if muted:
        current.add(rule_code)
    else:
        current.discard(rule_code)
    task.params = dict(task.params or {})
    task.params[MUTE_PARAM] = sorted(current)
    task.save(update_fields=['params', 'updated_at'])
    return sorted(current)


def evaluate(user, today=None, skip=None):
    """跑全部未屏蔽规则，返回命中的提醒列表。单条规则报错不影响其它条。"""
    from django.utils import timezone

    today = today or timezone.localdate()
    skip = set(skip if skip is not None else muted_rules())
    alerts = []
    for code, fn in RULES.items():
        if code in skip:
            continue
        try:
            alert = fn(user, today)
        except Exception:  # noqa: BLE001 - 规则是"锦上添花"，绝不能反噬主流程
            alert = None
        if alert:
            alerts.append(alert)
    return alerts
