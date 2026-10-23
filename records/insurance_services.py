"""保险标的/保单年度挂接服务（阶段 2）。

归一口径与 records.insurance_grouping 完全一致：任何一条保单流水保存后
调用 :func:`attach_record`，按标题归一出标的（车牌或险种），按截止年份
归并保单年度，年度冗余汇总字段由明细统一重算。

设计原则：
- 幂等：attach_record 可重复调用，结果一致；
- 自愈：编辑导致标题/日期变化时自动改挂，旧年度重算，空年度删除；
- 安全：标的/年度的并发创建依赖数据库唯一约束兜底（get_or_create）。
"""

from __future__ import annotations

import logging
from decimal import Decimal

from django.utils import timezone

from .insurance_grouping import normalize_insurer, subject_of
from .models import InsurancePolicyYear, InsuranceSubject

logger = logging.getLogger(__name__)


def _get_or_create_subject(meta):
    """按归一元数据取或建标的；优先使用已有默认部门。"""
    defaults = {
        'kind': meta['kind'],
        'label': meta['label'],
        'plate': meta['plate'],
        'insurance_type': meta['insurance_type'],
    }
    if meta['kind'] == 'car':
        return InsuranceSubject.objects.get_or_create(
            plate=meta['plate'], defaults=defaults,
        )[0]
    return InsuranceSubject.objects.get_or_create(
        kind='non_car', insurance_type=meta['insurance_type'], defaults=defaults,
    )[0]


def recompute_year(policy_year, save=True):
    """从明细重算保单年度的起止日期、保费合计、保司（归一）。"""
    records = list(policy_year.records.all())
    if not records:
        return False
    policy_year.start_date = min(r.issue_date for r in records)
    policy_year.end_date = max(r.end_date for r in records)
    policy_year.premium = sum((r.premium for r in records), Decimal('0'))
    policy_year.insurer = '、'.join(
        sorted({normalize_insurer(r.company_name) for r in records})
    )
    if save:
        policy_year.save(update_fields=[
            'start_date', 'end_date', 'premium', 'insurer', 'updated_at',
        ])
    return True


def cleanup_year(year_id):
    """重算指定年度；年度下已无明细时删除该年度。"""
    if not year_id:
        return
    try:
        policy_year = InsurancePolicyYear.objects.get(pk=year_id)
    except InsurancePolicyYear.DoesNotExist:
        return
    if not policy_year.records.exists():
        policy_year.delete()
        return
    recompute_year(policy_year)


def attach_record(record):
    """把保单记录挂接到标的与保单年度（标的/年度不存在则创建）。

    编辑导致归属变化时，旧年度自动重算，空年度自动删除。
    返回 (subject, policy_year)。
    """
    meta = subject_of(record)
    subject = _get_or_create_subject(meta)
    old_year_id = record.policy_year_id

    policy_year = InsurancePolicyYear.objects.filter(
        subject=subject, year=record.end_date.year,
    ).first()
    if policy_year is None:
        policy_year = InsurancePolicyYear.objects.create(
            subject=subject, year=record.end_date.year,
            start_date=record.issue_date, end_date=record.end_date,
        )

    if record.subject_id != subject.id or record.policy_year_id != policy_year.id:
        record.subject_id = subject.id
        record.policy_year_id = policy_year.id
        record.save(update_fields=['subject', 'policy_year'])

    recompute_year(policy_year)
    if old_year_id and old_year_id != policy_year.id:
        cleanup_year(old_year_id)
    return subject, policy_year


def attach_record_safe(record):
    """attach_record 的保护版：失败仅记日志，不影响保存主流程。"""
    try:
        return attach_record(record)
    except Exception:  # noqa: BLE001 - 挂接失败不得阻断业务保存
        logger.exception('保险记录 #%s 标的挂接失败', record.pk)
        return None, None


def detach_record(record):
    """记录删除后调用：重算/清理其原属年度。"""
    cleanup_year(record.policy_year_id)


def subject_key(subject):
    """标的主档对应的聚合键（与 insurance_grouping 的 key 口径一致）。"""
    if subject.kind == 'car':
        return f'car:{subject.plate}'
    return f'noncar:{subject.insurance_type}'


def subject_by_key(key):
    """按聚合键取标的主档，供一次性标记等操作使用。"""
    if key.startswith('car:'):
        return InsuranceSubject.objects.filter(kind='car', plate=key[4:]).first()
    if key.startswith('noncar:'):
        return InsuranceSubject.objects.filter(
            kind='non_car', insurance_type=key[7:]).first()
    return None


def one_off_keys():
    """已标记为一次性保险的标的聚合键集合。"""
    return {
        (f'car:{s.plate}' if s.kind == 'car' else f'noncar:{s.insurance_type}')
        for s in InsuranceSubject.objects.filter(is_one_off=True)
    }


def renewal_alerts(days=30, today=None):
    """按标的汇总临期待续与应续未续（阶段 3 预警口径）。

    以每个标的的最新保单年度为准，避免同年度多条保单重复提醒：
    - 应续未续：最新年度截止日早于今天（之后没有新的年度覆盖今日）；
    - 临期待续：最新年度覆盖今天，且剩余天数不超过 ``days``。

    返回 {'due': [...], 'lapsed': [...], ...}；每个元素含 subject/year/record
    与 days_left、overdue_days，供定时提醒与页面展示共用。
    """

    today = today or timezone.localdate()
    due, lapsed = [], []
    for subject in InsuranceSubject.objects.filter(is_one_off=False):
        year = subject.policy_years.order_by('-end_date', '-id').first()
        if year is None:
            continue
        record = year.records.order_by('-end_date', '-id').first()
        if record is None:
            continue
        item = {
            'subject': subject,
            'year': year,
            'record': record,
            'days_left': (year.end_date - today).days,
            'overdue_days': max(0, (today - year.end_date).days),
        }
        if year.end_date < today:
            lapsed.append(item)
        elif item['days_left'] <= days:
            due.append(item)

    due.sort(key=lambda i: (i['days_left'], i['subject'].label))
    lapsed.sort(key=lambda i: (-i['overdue_days'], i['subject'].label))
    return {
        'due': due,
        'lapsed': lapsed,
        'days': days,
        'today': today,
        'due_count': len(due),
        'lapsed_count': len(lapsed),
        'due_premium': sum((i['year'].premium for i in due), Decimal('0')),
    }


def renewal_initial(record):
    """续保预填数据：沿用上期保司/标题/部门/备注/保费，日期顺推一年。"""
    from datetime import timedelta

    start = record.end_date + timedelta(days=1)
    try:
        end = start.replace(year=start.year + 1) - timedelta(days=1)
    except ValueError:  # 2 月 29 日
        end = start.replace(year=start.year + 1, day=28) - timedelta(days=1)
    return {
        'company_name': record.company_name,
        'title': record.title,
        'order_number': '',
        'premium': record.premium,
        'issue_date': start,
        'end_date': end,
        'department': record.department,
        'remarks': record.remarks,
    }
