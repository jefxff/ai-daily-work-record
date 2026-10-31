"""每日业务简报：把"今天该关注的几件事"拼成一段人读的话。

设计取舍（三条，都很实际）：

1. **只做确定性统计，模型不参与数字**。数字算错一次，用户对整个助手的信任
   就没了；而这几行的复杂度根本用不上模型——去重后的计数 + 模板句而已。
2. **每段单独容错**。某一段依赖的模型/表报错（模块不存在、权限异常），
   其余段落照常输出；缺的一段标注"暂不可用"，而不是整封简报不生成。
3. **生成的结果是"会话"，不是"邮件正文"**。写进 CopilotConversation 后，
   用户点通知进来就能直接追问（"第 2 条细说一下"）——这是简报真正的价值：
   把一次性推送变成对话的起点。
"""
from datetime import timedelta

from django.utils import timezone

from accounts.services import has_perm

EMPTY_NOTE = '（这一段的数据暂时取不到，可能是对应模块没有可用数据。）'


def _section(title, lines, note=''):
    return {'title': title, 'lines': lines or [], 'note': note}


def _compose_markdown(sections, today):
    """把各段拼成 Markdown（面板用同样的规则渲染）。"""
    parts = [f'## 今日需要注意（{today:%Y-%m-%d}）', '']
    total = sum(len(s['lines']) for s in sections if not s['note'])
    if not total:
        parts.append('今天没有需要立即处理的事项。')
        parts.append('')
        parts.append('> 有想细看的方向可以直接问，比如"最近一个月新增的客诉有哪些"。')
        return '\n'.join(parts)

    for section in sections:
        if section['note']:
            continue
        if not section['lines']:
            continue
        parts.append(f'### {section["title"]}（{len(section["lines"])} 条）')
        parts.append('')
        for line in section['lines']:
            parts.append(f'- {line}')
        parts.append('')
    for section in sections:
        if section['note']:
            parts.append(f'### {section["title"]}')
            parts.append('')
            parts.append(section['note'])
            parts.append('')
    parts.append('---')
    parts.append('')
    parts.append('以上由本地规则统计，未调用外部模型。想展开哪一条，直接在下面追问即可。')
    return '\n'.join(parts)


def _expiry_section(user, today):
    """到期/逾期：复用到期预警页同一套收集逻辑，保证两个页面说的数是同一个。"""
    from records import expiry

    items = expiry.collect_expiry_items(user, horizon_days=7, today=today)
    # 借阅应还单独成段（它就是"超期未还"），这里必须剔掉，
    # 否则同一份档案会在两处各出现一次，看起来像两件事。
    items = [it for it in items if it['kind'] != 'borrow_due']
    overdue = [it for it in items if it['days_left'] < 0]
    soon = [it for it in items if 0 <= it['days_left'] <= 7]
    lines = []
    for it in overdue[:3]:
        lines.append(f'**已逾期** {it["kind_label"]}：{it["title"]}'
                     f'（逾期 {abs(it["days_left"])} 天，{it["subtitle"] or "—"}）')
    for it in soon[:5]:
        when = f'{it["days_left"]} 天后' if it['days_left'] else '今天'
        lines.append(f'{it["kind_label"]}：{it["title"]}（{when}到期）')
    return _section('到期与逾期', lines)


def _complaint_section(user, today):
    """近 7 天新增客诉（按 客诉申请日期 计，不是录入时间）。"""
    if not has_perm(user, 'customer_complaint', 'read'):
        return None
    from records.models import CustomerComplaint

    since = today - timedelta(days=7)
    rows = (CustomerComplaint.objects
            .filter(complaint_date__gte=since)
            .order_by('-complaint_date')[:5])
    lines = []
    for row in rows:
        status = row.complaint_status or '未标注'
        lines.append(f'{row.customer_name}（{row.complaint_date:%m-%d}，{status}）'
                     f'· {row.responsible_salesperson or "—"}')
    return _section('近 7 天新增客诉', lines)


def _daily_record_section(user, today):
    """本人名下未完成的日报，且到期日临近或已过。"""
    if not has_perm(user, 'daily_record', 'read'):
        return None
    from work_record.models import DailyRecord

    rows = (DailyRecord.objects
            .filter(user=user, status='未完成')
            .exclude(progress__gte=100)
            .order_by('due_date')[:5])
    lines = []
    for row in rows:
        when = row.due_date
        tail = f'，截止 {when:%m-%d}' if when else ''
        overdue = when and when < today
        mark = '**已超期** ' if overdue else ''
        lines.append(f'{mark}{row.department}：{(row.work_content or "")[:40]}'
                     f'{tail}（进度 {row.progress}%）')
    return _section('未完成的日报', lines)


def _borrow_section(user, today):
    """档案借阅超期：借阅人里可能有系统外人员，这里只按"未归还且超期"提示。"""
    if not has_perm(user, 'archive', 'read'):
        return None
    from archive.models import BorrowRecord

    rows = (BorrowRecord.objects
            .filter(return_time__isnull=True, due_time__lt=today)
            .select_related('archive')
            .order_by('due_time')[:5])
    lines = []
    for row in rows:
        days = (today - row.due_time).days
        lines.append(f'{row.archive.name}（{row.archive.code}）· 借阅人 {row.borrower}'
                     f'，超期 {days} 天')
    return _section('借阅超期未还', lines)


SECTIONS = (
    _expiry_section,
    _complaint_section,
    _daily_record_section,
    _borrow_section,
)


def build(user, today=None):
    """生成某位用户的简报：返回 ``(markdown, sections)``。

    单段异常被兜住并退化为 EMPTY_NOTE：宁可少一段，也不要整封发不出去。
    """
    today = today or timezone.localdate()
    sections = []
    for builder in SECTIONS:
        try:
            section = builder(user, today)
        except Exception:  # noqa: BLE001 - 见模块 docstring 第 2 条
            section = None
        if section is None:      # 无权限：直接不出现，别占地方
            continue
        if not section['lines'] and not section['note']:
            continue
        sections.append(section)
    if not sections:
        sections = [_section('今日概览', [], EMPTY_NOTE)]
    return _compose_markdown(sections, today), sections


def headline(sections):
    """一句话摘要（通知标题/列表用）："逾期 2 项 · 新增客诉 3 条 …"。 """
    bits = []
    for section in sections:
        if section['note'] or not section['lines']:
            continue
        bits.append(f'{section["title"]} {len(section["lines"])} 条')
    return ' · '.join(bits) if bits else '今天没有需要立即处理的事项'


def save_conversation(user, markdown, today=None):
    """把简报落成一个会话，供用户点进去追问。

    每份简报单独一个会话（按日期去重）：否则追问会和前几天的混在一起，
    "第 2 条"这种指代就没法解了。
    """
    from .models import CopilotConversation, CopilotMessage

    today = today or timezone.localdate()
    title = f'每日简报 {today:%m-%d}'
    conversation, created = CopilotConversation.objects.get_or_create(
        user=user, title=title, status=CopilotConversation.Status.ACTIVE,
        defaults={'title': title},
    )
    if created or not conversation.messages.exists():
        CopilotMessage.objects.create(
            conversation=conversation,
            role=CopilotMessage.Role.ASSISTANT,
            content=markdown,
        )
    conversation.save(update_fields=['last_active_at'])
    return conversation
