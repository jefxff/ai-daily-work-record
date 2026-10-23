"""保险只读聚合服务（阶段 1）。

不修改数据库、不写任何业务数据：把现有 ``Insurance`` 流水记录按
「车牌 + 保单年度」或「险种 + 保单年度」重新聚合，供列表、详情、
续保报告三个只读页面使用。

口径说明：
- 车险：标题中提取车牌（粤A~粤Z + 5~6 位），一个车牌 = 一个标的；
- 非车险：按归一后的险种归类，一个险种 = 一个标的；
- 保单年度：按保单截止日期所在年份分组，同一投保期间的多个条目
  （主险/附加险/多标的）归入同一年度；
- 断档：下一保单年度开始日期晚于上一保单年度结束日期 + 1 天时，
  中间无保障的自然日数量。
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from django.utils import timezone
from decimal import Decimal


PLATE_RE = re.compile(r'粤[A-Z][A-Z0-9]{5,6}')

# 保司名称归一：按关键词匹配，顺序即优先级（长关键词在前）
INSURER_RULES = (
    ('中国太平洋', '中国太平洋财产保险'),
    ('太平洋', '中国太平洋财产保险'),
    ('中国人民财产', '中国人民财产保险'),
    ('人保', '中国人民财产保险'),
    ('中国平安', '中国平安财产保险'),
    ('平安', '中国平安财产保险'),
    ('中国人寿', '中国人寿财产保险'),
    ('人寿', '中国人寿财产保险'),
    ('华泰', '华泰财产保险'),
    ('中银', '中银保险'),
    ('国任', '国任财产保险'),
)

# 险种归一：按关键词匹配，顺序即优先级
TYPE_RULES = (
    ('车险', '车险'),
    ('高新技术企业产品责任', '产品责任险'),
    ('产品责任', '产品责任险'),
    ('高新技术企业产品质量保证', '产品质量保证险'),
    ('产品质量保证', '产品质量保证险'),
    ('董监事', '董监事及高管责任险'),
    ('董监高', '董监事及高管责任险'),
    ('财产一切险', '财产一切险'),
    ('公众责任险', '公众责任险'),
    ('营业中断险', '营业中断险'),
    ('安全生产责任', '安全生产责任险'),
    ('财产综合险', '财产综合险'),
    ('建工无忧', '建筑工程险'),
    ('意外伤害', '意外伤害及健康险'),
    ('诉讼财产保全', '诉讼财产保全责任险'),
)

# 标题中的单据类后缀，归一险种时剔除（长词在前，避免「投保单」被「保单」截断）
_DOC_WORDS_RE = re.compile(
    r'(投保单|保险单|缴费通知书|保险合同|保单|合同|&|、|,|，|\s)+'
)

STATUS_RANK = {'expired': 0, 'expiring': 1, 'pending': 2, 'active': 3}


@dataclass
class PolicyYear:
    """一个保单年度（同一截止年份下的全部保单条目）。"""

    year: int
    records: list
    start: date
    end: date
    premium: Decimal
    insurers: list
    insurer_names: list
    departments: list
    status: str
    status_label: str
    days_left: int
    record_count: int
    record_ids: list
    continuity: str = 'first'          # first / ok / gap / overlap
    continuity_label: str = '首年'
    gap_days_before: int = 0
    overlap_days: int = 0
    yoy: Decimal | None = None
    yoy_pct: float | None = None
    insurer_changed: bool = False
    prev_insurers: list = field(default_factory=list)
    is_current: bool = False

    @property
    def primary_record(self):
        """该年度内保单期间最晚的一条，作为「当前保单」入口。"""
        return max(self.records, key=lambda r: (r.end_date, r.id))


@dataclass
class InsuranceSubject:
    """保险标的：一台车，或一个非车险险种。"""

    key: str
    kind: str                          # car / non_car
    kind_label: str
    label: str
    plate: str
    insurance_type: str
    terms: list
    latest: PolicyYear
    previous: PolicyYear | None
    status: str
    status_label: str
    days_left: int
    expired_days: int
    has_gap: bool
    max_gap_days: int
    gap_terms: list
    has_overlap: bool
    overlap_terms: list
    yoy: Decimal | None
    yoy_pct: float | None
    insurer_changed: bool
    record_count: int
    term_count: int
    departments: list

    @property
    def latest_insurer(self):
        return '、'.join(self.latest.insurers) if self.latest.insurers else '—'

    @property
    def badge_class(self):
        return {'expired': 'danger', 'expiring': 'warning', 'pending': 'info'}.get(
            self.status, 'success'
        )

    @property
    def kind_badge_class(self):
        return 'info' if self.kind == 'car' else 'default'

    @property
    def yoy_class(self):
        if self.yoy is None or self.yoy == 0:
            return ''
        return 'ins-up' if self.yoy > 0 else 'ins-down'

    @property
    def latest_period(self):
        return f'{self.latest.start:%Y-%m-%d} ~ {self.latest.end:%Y-%m-%d}'

    @property
    def yoy_label(self):
        if self.yoy is None:
            return '—'
        sign = '+' if self.yoy > 0 else ''
        if self.yoy_pct is None:
            return f'{sign}{self.yoy}'
        return f'{sign}{self.yoy_pct:.1f}%'


def extract_plate(title):
    """从标题中提取车牌号；没有则返回空串。"""
    match = PLATE_RE.search(title or '')
    return match.group(0) if match else ''


def normalize_insurer(name):
    """保司名称归一，无法识别时保留原值。"""
    raw = (name or '').strip()
    for needle, canonical in INSURER_RULES:
        if needle in raw:
            return canonical
    return raw or '未知保司'


def normalize_insurance_type(title, plate=''):
    """险种归一；车险统一为「车险」，非车险按关键词归并。"""
    if plate:
        return '车险'
    text = (title or '').strip()
    for needle, canonical in TYPE_RULES:
        if needle in text:
            return canonical
    cleaned = _DOC_WORDS_RE.sub('', text).strip(' -_·')
    return cleaned or '其他保险'


def subject_of(record):
    """返回记录的标的键与展示信息。"""
    plate = extract_plate(record.title)
    if plate:
        return {
            'key': f'car:{plate}',
            'kind': 'car',
            'kind_label': '车险',
            'label': plate,
            'plate': plate,
            'insurance_type': '车险',
        }
    insurance_type = normalize_insurance_type(record.title)
    return {
        'key': f'noncar:{insurance_type}',
        'kind': 'non_car',
        'kind_label': '非车险',
        'label': insurance_type,
        'plate': '',
        'insurance_type': insurance_type,
    }


def _build_year(year, records, today):
    ordered = sorted(records, key=lambda r: (r.issue_date, r.end_date, r.id))
    start = min(r.issue_date for r in ordered)
    end = max(r.end_date for r in ordered)
    premium = sum((r.premium for r in ordered), Decimal('0'))
    insurers = sorted({normalize_insurer(r.company_name) for r in ordered})
    insurer_names = sorted({(r.company_name or '').strip() for r in ordered if r.company_name})
    departments = sorted({(r.department or '').strip() for r in ordered if r.department})

    if today < start:
        status, status_label = 'pending', '待生效'
    elif today > end:
        status, status_label = 'expired', '已过期'
    else:
        status, status_label = 'active', '在保'

    return PolicyYear(
        year=year,
        records=ordered,
        start=start,
        end=end,
        premium=premium,
        insurers=insurers,
        insurer_names=insurer_names,
        departments=departments,
        status=status,
        status_label=status_label,
        days_left=(end - today).days,
        record_count=len(ordered),
        record_ids=[r.id for r in ordered],
    )


def _annotate_terms(terms):
    """补齐年度之间的衔接、同比、保司变更、状态文案。"""
    previous = None
    for term in terms:
        if previous is None:
            term.continuity = 'first'
            term.continuity_label = '首年'
        else:
            delta = (term.start - previous.end).days
            if delta > 1:
                term.gap_days_before = delta - 1
                term.continuity = 'gap'
                term.continuity_label = f'断档 {term.gap_days_before} 天'
            elif delta <= 0:
                term.overlap_days = (previous.end - term.start).days + 1
                term.continuity = 'overlap'
                term.continuity_label = f'与上期重叠 {term.overlap_days} 天'
            else:
                term.continuity = 'ok'
                term.continuity_label = '无缝衔接'

            if previous.premium > 0:
                term.yoy = term.premium - previous.premium
                term.yoy_pct = float(term.yoy / previous.premium * 100)
            term.insurer_changed = set(term.insurers) != set(previous.insurers)
            term.prev_insurers = previous.insurers

        if term.status == 'expired' and previous is not None:
            term.status_label = '已续保'
        previous = term

    # 最新年度：当前有效/即将到期/应续未续都以它为准
    latest = terms[-1]
    latest.is_current = True
    if latest.status == 'active':
        if latest.days_left <= 30:
            latest.status_label = '即将到期'
        else:
            latest.status_label = '在保'
    elif latest.status == 'pending':
        latest.status_label = '待生效'
    else:
        latest.status_label = '已过期'
    return latest


def _build_subject(key, meta, records, today):
    by_year = OrderedDict()
    for record in sorted(records, key=lambda r: (r.issue_date, r.end_date, r.id)):
        by_year.setdefault(record.end_date.year, []).append(record)

    terms = [_build_year(year, recs, today) for year, recs in by_year.items()]
    latest = _annotate_terms(terms)
    previous = terms[-2] if len(terms) > 1 else None

    gap_terms = [t for t in terms if t.continuity == 'gap']
    overlap_terms = [t for t in terms if t.continuity == 'overlap']
    max_gap_days = max((t.gap_days_before for t in gap_terms), default=0)

    if latest.status == 'expired':
        status, status_label = 'expired', '应续未续'
        days_left, expired_days = 0, (today - latest.end).days
    elif latest.status == 'pending':
        status, status_label = 'pending', '待生效'
        days_left, expired_days = latest.days_left, 0
    elif latest.days_left <= 30:
        status, status_label = 'expiring', '即将到期'
        days_left, expired_days = latest.days_left, 0
    else:
        status, status_label = 'active', '在保'
        days_left, expired_days = latest.days_left, 0

    yoy = latest.yoy
    yoy_pct = latest.yoy_pct
    insurer_changed = latest.insurer_changed
    departments = sorted({d for t in terms for d in t.departments})

    return InsuranceSubject(
        key=key,
        kind=meta['kind'],
        kind_label=meta['kind_label'],
        label=meta['label'],
        plate=meta['plate'],
        insurance_type=meta['insurance_type'],
        terms=terms,
        latest=latest,
        previous=previous,
        status=status,
        status_label=status_label,
        days_left=days_left,
        expired_days=expired_days,
        has_gap=bool(gap_terms),
        max_gap_days=max_gap_days,
        gap_terms=gap_terms,
        has_overlap=bool(overlap_terms),
        overlap_terms=overlap_terms,
        yoy=yoy,
        yoy_pct=yoy_pct,
        insurer_changed=insurer_changed,
        record_count=sum(t.record_count for t in terms),
        term_count=len(terms),
        departments=departments,
    )


def build_subjects(records, today=None):
    """把保险流水聚合成标的列表（按紧急程度排序）。"""
    today = today or timezone.localdate()
    buckets = OrderedDict()
    for record in records:
        meta = subject_of(record)
        bucket = buckets.setdefault(meta['key'], {'meta': meta, 'records': []})
        bucket['records'].append(record)

    subjects = [
        _build_subject(key, bucket['meta'], bucket['records'], today)
        for key, bucket in buckets.items()
    ]
    return sort_subjects(subjects)


def subject_for_record(record, records, today=None):
    """找到某条保单记录所属的标的（详情页续保时间线用）。"""
    key = subject_of(record)['key']
    for subject in build_subjects(records, today=today):
        if subject.key == key:
            return subject
    return None


def sort_subjects(subjects, sort='expiry'):
    """排序：expiry=按紧急程度；plate=按标的名称；premium=按当前保费倒序。"""
    if sort == 'plate':
        return sorted(subjects, key=lambda s: (s.kind != 'car', s.label))
    if sort == 'premium':
        return sorted(subjects, key=lambda s: (-s.latest.premium, s.label))
    return sorted(
        subjects,
        key=lambda s: (
            STATUS_RANK.get(s.status, 9),
            0 if s.has_gap else 1,
            s.latest.end,
            s.label,
        ),
    )


def summarize(subjects):
    """汇总卡片数据。"""
    active = [s for s in subjects if s.status in ('active', 'expiring')]
    return {
        'total': len(subjects),
        'car': sum(1 for s in subjects if s.kind == 'car'),
        'non_car': sum(1 for s in subjects if s.kind == 'non_car'),
        'active': len(active),
        'expiring': sum(1 for s in subjects if s.status == 'expiring'),
        'expired': sum(1 for s in subjects if s.status == 'expired'),
        'pending': sum(1 for s in subjects if s.status == 'pending'),
        'gap': sum(1 for s in subjects if s.has_gap),
        'overlap': sum(1 for s in subjects if s.has_overlap),
        'current_premium': sum((s.latest.premium for s in active), Decimal('0')),
    }


def filter_subjects(subjects, q='', kind='', status=''):
    """列表页筛选：关键词 / 标的类型 / 状态。"""
    result = subjects
    if kind in ('car', 'non_car'):
        result = [s for s in result if s.kind == kind]
    if status == 'active':
        result = [s for s in result if s.status in ('active', 'expiring')]
    elif status == 'expiring':
        result = [s for s in result if s.status == 'expiring']
    elif status == 'expired':
        result = [s for s in result if s.status == 'expired']
    elif status == 'pending':
        result = [s for s in result if s.status == 'pending']
    elif status == 'gap':
        result = [s for s in result if s.has_gap]
    elif status == 'overlap':
        result = [s for s in result if s.has_overlap]

    needle = (q or '').strip().lower()
    if needle:
        def _hit(subject):
            parts = [
                subject.label, subject.insurance_type, subject.kind_label,
                *(subject.latest.insurers or []), *(subject.departments or []),
            ]
            parts.extend(r.title or '' for t in subject.terms for r in t.records)
            parts.extend(r.order_number or '' for t in subject.terms for r in t.records)
            return needle in ' '.join(parts).lower()
        result = [s for s in result if _hit(s)]
    return result


def build_report(subjects):
    """续保报告数据：应续未续 / 断档 / 即将到期 / 数据提示。"""
    should_renew = sorted(
        [s for s in subjects if s.status == 'expired'],
        key=lambda s: (-s.expired_days, s.label),
    )
    gaps = sorted(
        [s for s in subjects if s.has_gap],
        key=lambda s: (-s.max_gap_days, s.label),
    )
    upcoming = sorted(
        [s for s in subjects if s.status == 'expiring'],
        key=lambda s: (s.latest.end, s.label),
    )
    overlaps = sorted(
        [s for s in subjects if s.has_overlap],
        key=lambda s: s.label,
    )

    missing_order = []
    multi_item_terms = []
    for subject in subjects:
        for term in subject.terms:
            for record in term.records:
                if not (record.order_number or '').strip():
                    missing_order.append(record)
            if term.record_count > 1:
                multi_item_terms.append((subject, term))

    multi_item_summary = []
    grouped = OrderedDict()
    for subject, term in multi_item_terms:
        entry = grouped.setdefault(subject.key, {'subject': subject, 'years': []})
        entry['years'].append(term)
    for entry in grouped.values():
        entry['year_count'] = len(entry['years'])
        entry['record_count'] = sum(t.record_count for t in entry['years'])
        multi_item_summary.append(entry)
    multi_item_summary.sort(key=lambda e: -e['record_count'])

    return {
        'should_renew': should_renew,
        'gaps': gaps,
        'upcoming': upcoming,
        'overlaps': overlaps,
        'missing_order': missing_order,
        'multi_item_terms': multi_item_terms,
        'multi_item_summary': multi_item_summary,
        'should_renew_count': len(should_renew),
        'gap_count': len(gaps),
        'upcoming_count': len(upcoming),
        'overlap_count': len(overlaps),
        'missing_order_count': len(missing_order),
        'multi_item_count': len(multi_item_terms),
    }
