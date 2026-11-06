"""诉讼案件周报（PDF）：取数、分组、本周更新内容与 PDF 渲染。

版式见「诉讼周报PDF_示例模板_20261106.md」：
- 顶部为业务句式的「本周更新内容」，下方注明「已剔除已结案」；
- 主表分「货款纠纷」「非货款纠纷」两组，货款在上、非货款另起一页；
- 表格 9 列，备注原文与立案/开庭/判决/执行日期并入备注列；
- 每组末尾为小计；页脚为页码、生成时间与数据来源。

本模块只读取 Complaint / OperationLog，不写业务数据。视图与后续每周一
自动落盘命令共用 build_context()，避免两处口径漂移。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.template.loader import render_to_string
from django.utils import timezone

from .models import Complaint, OperationLog

GOODS_DISPUTE = '货款纠纷'
CLOSED_PROGRESS = '结案'
AUDIT_MODULE = '诉讼'
# 导出动作只记录「谁导出了报表」，不属于案件本身的更新
EXCLUDED_OPERATION_TYPES = ('导出',)
# 顶部更新清单最多列 N 条，避免撑爆第 1 页把主表挤到下一页
MAX_UPDATE_ITEMS = 12

# 留痕快照里由状态推进自动回填的日期、以及主键，不作为「本周跟进」逐条列出
IGNORE_DIFF_FIELDS = {
    'id', 'filed_at', 'hearing_at', 'judgment_at', 'execution_at', 'closed_at',
}

FIELD_LABELS = {
    'dispute_type': '纠纷类型', 'plaintiff': '原告', 'defendant': '被告',
    'case_number': '案号', 'amount': '涉案金额', 'court': '受理法院',
    'lawyer': '代理律师', 'progress': '进度',
    'detailed_progress': '详细进度', 'remarks': '备注',
    'filed_at': '立案日期', 'hearing_at': '开庭日期',
    'judgment_at': '判决日期', 'execution_at': '执行日期', 'closed_at': '结案日期',
}

# 备注列里附带的业务日期，顺序与示例模板一致
NOTE_DATE_FIELDS = (
    ('立案', 'filed_at'), ('开庭', 'hearing_at'),
    ('判决', 'judgment_at'), ('执行', 'execution_at'),
)

EMPTY_TEXTS = {'', '暂无', 'None', 'null', '—', '-'}

_CASE_ID_RE = re.compile(r'#(\d+)')
_IMPORT_RE = re.compile(r'新增\s*(\d+)\s*更新\s*(\d+)\s*跳过\s*(\d+)')

DATA_SOURCE = '数据来源：诉讼台账（records.Complaint）· 内部资料'


@dataclass(frozen=True)
class WeekWindow:
    """统计窗口：留痕取 [start, end_exclusive)，标题展示 label_start ~ label_end。"""

    start: datetime
    end_exclusive: datetime
    label_start: date
    label_end: date

    @property
    def label(self):
        return f'{self.label_start.isoformat()} ~ {self.label_end.isoformat()}'


def _midnight(day):
    return timezone.make_aware(datetime.combine(day, time.min))


def resolve_window(*, week='current', as_of=None, start=None, end=None):
    """计算统计窗口。

    - week='current'（默认，手动下载）：本周一 00:00 ~ 下周一 00:00，
      标题写本周一 ~ 本周日，留痕只取到当前时刻，天然不含未来数据。
    - week='last'（供每周一自动任务）：上周一 00:00 ~ 本周一 00:00。
    - start/end：ISO 日期字符串，指定任意窗口（end 省略时按 7 天计）。
    """
    if start:
        label_start = date.fromisoformat(start)
        label_end = date.fromisoformat(end) if end else label_start + timedelta(days=6)
        if label_end < label_start:
            raise ValueError('结束日期不能早于开始日期')
        return WeekWindow(_midnight(label_start), _midnight(label_end + timedelta(days=1)),
                          label_start, label_end)

    now = as_of or timezone.localtime(timezone.now())
    this_monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    if week == 'last':
        monday = this_monday - timedelta(days=7)
        return WeekWindow(monday, this_monday, monday.date(),
                          this_monday.date() - timedelta(days=1))
    if week not in ('current', 'last'):
        raise ValueError('week 只能是 current / last')
    return WeekWindow(this_monday, this_monday + timedelta(days=7),
                      this_monday.date(), this_monday.date() + timedelta(days=6))


def _text(value):
    """快照值归一：None / 字符串 'None' / 占位符一律视为空。用于口径判定。"""
    if value is None:
        return ''
    text = str(value).strip()
    return '' if text in EMPTY_TEXTS else text


def _display(value):
    """表格展示值：保留「暂无」等原文，只有真正空值才显示破折号。"""
    if value is None or str(value).strip() in ('', 'None', 'null'):
        return '—'
    return str(value).strip()


def _raw(value):
    """更正句式里的原文：保留「暂无」，仅把 None / 'None' 归一为「空」。"""
    if value is None or str(value).strip() in ('', 'None', 'null'):
        return '空'
    return str(value).strip()


def _money(value):
    try:
        return f'{Decimal(str(value or 0)):,.2f}'
    except (InvalidOperation, ValueError, TypeError):
        return '0.00'


def _sum_money(rows):
    total = Decimal('0')
    for row in rows:
        try:
            total += Decimal(str(row.amount or 0))
        except (InvalidOperation, ValueError, TypeError):
            continue
    return f'{total:,.2f}'


def _case_labels(logs):
    ids = set()
    for log in logs:
        match = _CASE_ID_RE.search(log.object_identifier or '')
        if match:
            ids.add(int(match.group(1)))
    return Complaint.objects.in_bulk(ids)


def _label_for(log, objects):
    """案件标识：优先案号，案号为占位符时用被告。

    记录已被删除时从留痕快照里取案号 / 被告，避免把「[诉讼]下的记录#N」
    这种内部标识写进周报。
    """
    match = _CASE_ID_RE.search(log.object_identifier or '')
    if match:
        obj = objects.get(int(match.group(1)))
        if obj:
            number = _text(obj.case_number)
            if number:
                return number
            defendant = _text(obj.defendant)
            if defendant:
                return defendant
        snapshot = log.after_json or log.before_json or {}
        number = _text(snapshot.get('case_number'))
        if number:
            return number
        defendant = _text(snapshot.get('defendant'))
        if defendant:
            return defendant
    return _text(log.object_identifier) or '诉讼记录'


def _build_updates(window):
    """把周窗口内的字段级留痕翻译成业务句式。

    顺序：跟进/进度类（时间倒序）→ 字段更正类（时间倒序）→ 汇总类（导入、新增）。
    导出留痕整批排除；状态推进自动回填的日期与主键不逐条列出。
    """
    logs = list(
        OperationLog.objects.filter(
            module_type=AUDIT_MODULE,
            operation_time__gte=window.start,
            operation_time__lt=window.end_exclusive,
        ).exclude(operation_type__in=EXCLUDED_OPERATION_TYPES)
        .order_by('operation_time')
    )
    objects = _case_labels(logs)
    follow_ups, corrections = [], []
    imports, additions, deletions = [], [], 0

    for log in logs:
        when = timezone.localtime(log.operation_time)
        label = _label_for(log, objects)
        operation = (log.operation_type or '').strip()

        if operation == '导入':
            match = _IMPORT_RE.search(log.change_summary or '')
            if match:
                imports.append(tuple(int(part) for part in match.groups()))
            continue

        if operation == '新增':
            after = log.after_json or {}
            additions.append({
                'case_number': _text(after.get('case_number')),
                'defendant': _text(after.get('defendant')),
                'amount': _money(after.get('amount')),
            })
            continue

        if operation == '删除':
            deletions += 1
            corrections.append((when, f'{label} 案件已删除'))
            continue

        diff = [
            (field, old, new)
            for field, old, new in (log.change_diff or [])
            if isinstance(field, str) and field not in IGNORE_DIFF_FIELDS
        ]
        if not diff:
            if _text(log.change_summary):
                corrections.append((when, f'{label} {_text(log.change_summary)}'))
            continue

        by_field = {field: (old, new) for field, old, new in diff}
        used = set()
        follow_up = None
        # 「所有的更新都会在备注里面列明」：备注优先，其次详细进度，最后状态推进
        if 'remarks' in by_field and _text(by_field['remarks'][1]):
            follow_up = f'{label} 案件更新进度：{_text(by_field["remarks"][1])}'
            used.add('remarks')
        elif 'detailed_progress' in by_field and _text(by_field['detailed_progress'][1]):
            follow_up = f'{label} 案件更新进度：{_text(by_field["detailed_progress"][1])}'
            used.add('detailed_progress')
        elif 'progress' in by_field:
            old, new = by_field['progress']
            follow_up = (
                f'{label} 案件更新进度：进度由「{_text(old) or "空"}」'
                f'变更为「{_text(new) or "空"}」'
            )
            used.add('progress')
        if follow_up:
            # 同一条留痕只写一句：备注 / 详细进度 / 状态推进已概括本次跟进，
            # 不再把同一批字段变更拆成「更正」，避免重复。
            follow_ups.append((when, follow_up))
            continue

        others = [
            (FIELD_LABELS.get(field, field), _raw(old), _raw(new))
            for field, old, new in diff
            if field not in used and field not in IGNORE_DIFF_FIELDS
            and _text(old) != _text(new)
        ]
        if others:
            detail = '；'.join(
                f'{name}由「{old}」更正为「{new}」' for name, old, new in others
            )
            corrections.append((when, f'{label} 案件更正：{detail}'))

    summary_parts = []
    if imports:
        created = sum(item[0] for item in imports)
        updated = sum(item[1] for item in imports)
        skipped = sum(item[2] for item in imports)
        summary_parts.append(
            f'本周导入 {len(imports)} 批：新增 {created} 条、更新 {updated} 条、跳过 {skipped} 条'
        )
    if additions:
        if len(additions) == 1:
            item = additions[0]
            summary_parts.append(
                f'新增起诉 {item["defendant"] or item["case_number"]}，涉及金额 ¥{item["amount"]}'
            )
        else:
            names = sorted({item['case_number'] or item['defendant'] for item in additions})
            amounts = {item['amount'] for item in additions}
            if len(names) == 1 and len(amounts) == 1:
                summary_parts.append(
                    f'新增起诉 {len(additions)} 条（{names[0]}，涉及金额各 ¥{amounts.pop()}）'
                )
            else:
                shown = '、'.join(names[:4]) + ('…' if len(names) > 4 else '')
                summary_parts.append(f'新增起诉 {len(additions)} 条（{shown}）')
    if deletions:
        summary_parts.append(f'删除 {deletions} 条')

    updates = [text for _, text in sorted(follow_ups, key=lambda item: item[0], reverse=True)]
    updates += [text for _, text in sorted(corrections, key=lambda item: item[0], reverse=True)]
    if summary_parts:
        updates.append('；'.join(summary_parts))

    if len(updates) > MAX_UPDATE_ITEMS:
        rest = len(updates) - MAX_UPDATE_ITEMS
        updates = updates[:MAX_UPDATE_ITEMS] + [f'其余 {rest} 条更新未列出']
    return updates


def _group_rows(rows):
    result = []
    for seq, obj in enumerate(rows, 1):
        note_lines = []
        remark = _display(obj.remarks)
        if remark != '—':
            note_lines.append(remark)
        for label, field in NOTE_DATE_FIELDS:
            value = getattr(obj, field, None)
            if value:
                note_lines.append(f'{label} {value.isoformat()}')
        result.append({
            'seq': seq,
            'case_number': _display(obj.case_number),
            'plaintiff': _display(obj.plaintiff),
            'defendant': _display(obj.defendant),
            'dispute_type': _display(obj.dispute_type),
            'amount': _money(obj.amount),
            'progress': _display(obj.progress),
            'court': _display(obj.court),
            'lawyer': _display(obj.lawyer),
            'detailed_progress': _display(obj.detailed_progress),
            'note_lines': note_lines or ['—'],
        })
    return result


def build_context(window=None, *, week='current', as_of=None, start=None, end=None):
    """组装打印页所需的全部数据。"""
    if window is None:
        window = resolve_window(week=week, as_of=as_of, start=start, end=end)

    all_rows = list(Complaint.objects.all().order_by('id'))
    open_rows = [row for row in all_rows if _text(row.progress) != CLOSED_PROGRESS]
    goods = [row for row in open_rows if _text(row.dispute_type) == GOODS_DISPUTE]
    others = [row for row in open_rows if _text(row.dispute_type) != GOODS_DISPUTE]

    groups = [
        {
            'index': '二',
            'heading': f'二、货款纠纷明细（{len(goods)} 条）',
            'rows': _group_rows(goods),
            'count': len(goods),
            'subtotal': f'¥{_sum_money(goods)}',
            'break_before': False,
        },
        {
            'index': '三',
            'heading': f'三、非货款纠纷明细（{len(others)} 条）',
            'rows': _group_rows(others),
            'count': len(others),
            'subtotal': f'¥{_sum_money(others)}',
            'break_before': True,
        },
    ]
    generated_at = (as_of or timezone.localtime(timezone.now())).strftime('%Y-%m-%d %H:%M')
    return {
        'title': f'诉讼案件周报（{window.label}）',
        'week_label': window.label,
        'generated_at': generated_at,
        'updates': _build_updates(window),
        'groups': groups,
        'open_count': len(open_rows),
        'goods_count': len(goods),
        'others_count': len(others),
        'excluded_closed': len(all_rows) - len(open_rows),
        'data_source': DATA_SOURCE,
        'window': window,
    }


def render_html(context):
    """渲染自包含打印页（CSS 内联，PDF 生成不依赖静态资源服务）。"""
    return render_to_string('records/complaint_weekly_pdf.html', context)


def render_pdf(html, generated_at):
    """用 Chromium 把打印页转成 A4 横向 PDF，零新增第三方依赖。"""
    from playwright.sync_api import sync_playwright

    footer = (
        '<div style="width:100%;font-family:\'Microsoft YaHei\',\'SimSun\',sans-serif;'
        'font-size:8px;color:#8b95a5;display:flex;justify-content:space-between;'
        'padding:3px 0 0;border-top:1px solid #e4e7ec;box-sizing:border-box;">'
        '<span>第 <span class="pageNumber"></span> 页 / 共 <span class="totalPages"></span> 页</span>'
        f'<span>生成 {generated_at} · {DATA_SOURCE}</span>'
        '</div>'
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(html, wait_until='load')
            return page.pdf(
                format='A4', landscape=True, print_background=True,
                display_header_footer=True,
                header_template='<span></span>', footer_template=footer,
                margin={'top': '9mm', 'bottom': '12mm', 'left': '11mm', 'right': '11mm'},
            )
        finally:
            browser.close()
