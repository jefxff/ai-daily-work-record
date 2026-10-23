"""数据看板视图（从 records/views.py 拆出，复核报告 ARC-03）。

只做「取统计 + 取明细 + 渲染」，统计口径统一在 records/stats.py。
"""

from datetime import date, datetime, timedelta

from django.db.models import Count
from django.db.models.functions import TruncDate
from django.shortcuts import render
from django.utils import timezone

from accounts.decorators import perm_required
from accounts.services import visible_modules

from . import insurance_grouping, insurance_services
from . import stats as stats_svc
from .models import Complaint, CustomerComplaint, CustomerCredit, DailyRecord, Decoration, Insurance
from .models import PriceEvaluation, Regulation
from .view_common import _my_daily_records, _weekly_trend


@perm_required("data_view", "read")
def dashboard(request):
    """数据展示看板视图"""
    visible = set(visible_modules(request.user))
    today_d = timezone.localdate()

    daily_records = _my_daily_records(request.user)
    customer_complaints = CustomerComplaint.objects.all()
    complaints = Complaint.objects.all()

    # 保险：标的聚合口径（与保险台账 /insurance/subjects/ 一致）
    _insurance_records = list(Insurance.objects.only(
        "id", "title", "issue_date", "end_date", "premium",
        "company_name", "department",
    ))
    _insurance_subjects = insurance_grouping.build_subjects(_insurance_records, today=today_d)
    _insurance_summary = insurance_grouping.summarize(_insurance_subjects)
    _one_off_keys = insurance_services.one_off_keys()
    insurance_expiring_count = sum(1 for s in _insurance_subjects if s.status == 'expiring')
    insurance_expired_count = sum(
        1 for s in _insurance_subjects
        if s.status == 'expired' and not _one_off_keys.intersection({s.key})
    )

    summary = stats_svc.dashboard_summary(
        request.user, visible,
        insurance_total=_insurance_summary['total'],
        insurance_active=_insurance_summary['active'],
        insurance_expired=insurance_expired_count,
    )
    total_decorations = summary['total_decorations']
    credit_pending_site_count = summary['credit_pending_site_count']
    complaint_incomplete_count = summary['complaint_incomplete_count']
    complaint_pending_count = summary['complaint_pending_count']
    lawsuit_incomplete_count = summary['lawsuit_incomplete_count']
    lawsuit_pending_count = summary['lawsuit_pending_count']
    daily_incomplete_count = summary['daily_incomplete_count']
    daily_pending_count = summary['daily_pending_count']
    price_total_count = summary['price_total_count']
    complaint_total_count = summary['complaint_total_count']
    lawsuit_total_count = summary['lawsuit_total_count']
    daily_total_count = summary['daily_total_count']

    # 待处理明细（列表展示，不参与统计口径）
    pending_items = []
    for record in daily_records.filter(status='未完成'):
        pending_items.append({
            'id': record.id,
            'type': 'daily',
            'type_name': '每日记录',
            'type_icon': '📝',
            'title': record.work_content[:50] + '...' if len(record.work_content) > 50 else record.work_content,
            'department': record.department,
            'created_at': record.record_date.strftime('%Y-%m-%d %H:%M'),
            'sort_at': record.record_date.timestamp(),
            'status': '未完成',
            'url': f'/daily_record/detail/{record.id}/'
        })

    for complaint in customer_complaints.filter(complaint_status='未完成'):
        pending_items.append({
            'id': complaint.id,
            'type': 'customer_complaint',
            'type_name': '客户客诉',
            'type_icon': '📞',
            'title': complaint.customer_name,
            'department': complaint.responsible_salesperson,
            'created_at': complaint.complaint_date.strftime('%Y-%m-%d'),
            'sort_at': datetime.combine(complaint.complaint_date, datetime.min.time()).timestamp(),
            'status': '未完成',
            'url': f'/customer_complaint/edit/{complaint.id}/'
        })

    for lawsuit in complaints.exclude(progress='结案'):
        pending_items.append({
            'id': lawsuit.id,
            'type': 'lawsuit',
            'type_name': '诉讼案件',
            'type_icon': '⚖️',
            'title': lawsuit.defendant,
            'department': lawsuit.lawyer,
            # 诉讼没有登记日期字段：不再把「立案中」塞进日期列（审计 #37）
            'created_at': '—',
            'sort_at': None,
            'status': lawsuit.progress,
            'url': f'/complaint/detail/{lawsuit.id}/'
        })

    # 有日期的按时间倒序在前；无日期项（诉讼）排在最后并保持 id 倒序
    dated = [i for i in pending_items if i['sort_at'] is not None]
    undated = [i for i in pending_items if i['sort_at'] is None]
    dated.sort(key=lambda x: x['sort_at'], reverse=True)
    pending_items = dated + undated

    weekly_trend = _weekly_trend(request.user)

    # 客诉：未闭环中已超 SLA 的数量（SLA 默认 48h，可按单覆盖）
    # 截止时间依赖每行 sla_hours，ORM 无法直接下推；只遍历未闭环子集并固定查询数。
    cc_open_qs = customer_complaints.filter(complaint_status__in=['未完成', '处理中'])
    complaint_sla_overdue_count = sum(1 for c in cc_open_qs if c.is_sla_overdue)

    # 诉讼：答辩期剩余 0~3 天（立案 + 15 天）
    lawsuit_answer_soon_count = 0
    for case in complaints.exclude(progress='结案'):
        answer_deadline = case.answer_deadline
        if answer_deadline and 0 <= (answer_deadline - today_d).days <= 3:
            lawsuit_answer_soon_count += 1

    insurance_expiring_subjects = [s for s in _insurance_subjects if s.status == 'expiring'][:5]

    # 每日记录：已过截止日期仍未完成
    daily_overdue_count = daily_records.filter(status='未完成', due_date__lt=today_d).count()

    module_badges = {
        'decoration': {'level': 'normal', 'text': '正常', 'url': ''},
        'customer_credit': {
            'level': 'warning' if credit_pending_site_count else 'normal',
            'text': f'{credit_pending_site_count} 项待排期' if credit_pending_site_count else '正常',
            'url': '',
        },
        'customer_complaint': {
            'level': 'error' if complaint_sla_overdue_count else ('warning' if complaint_incomplete_count else 'normal'),
            'text': (
                f'{complaint_sla_overdue_count} 单超时' if complaint_sla_overdue_count
                else (f'{complaint_incomplete_count} 单未闭环' if complaint_incomplete_count else '全部闭环')
            ),
            'url': '/workspace/?source=customer_complaint',
        },
        'complaint': {
            'level': 'error' if lawsuit_answer_soon_count else ('warning' if lawsuit_incomplete_count else 'normal'),
            'text': (
                f'{lawsuit_answer_soon_count} 项答辩临期' if lawsuit_answer_soon_count
                else (f'{lawsuit_incomplete_count} 件进行中' if lawsuit_incomplete_count else '全部结案')
            ),
            'url': '/workspace/?source=lawsuit',
        },
        'regulation': {'level': 'normal', 'text': '正常', 'url': ''},
        'price_evaluation': {'level': 'normal', 'text': '正常', 'url': ''},
        'insurance': {
            'level': 'error' if insurance_expired_count else ('warning' if insurance_expiring_count else 'normal'),
            'text': (
                f'{insurance_expired_count} 个应续未续' if insurance_expired_count
                else (f'{insurance_expiring_count} 个临期' if insurance_expiring_count else '正常')
            ),
            'url': '',
        },
        'daily_record': {
            'level': 'error' if daily_overdue_count else ('warning' if daily_incomplete_count else 'normal'),
            'text': (
                f'{daily_overdue_count} 条逾期' if daily_overdue_count
                else (f'{daily_incomplete_count} 条未完成' if daily_incomplete_count else '已清')
            ),
            'url': '/workspace/?source=daily_record',
        },
    }

    # 各模块记录数（仅统计当前用户可见模块，口径与 total_records 一致）
    module_chart_items = [('每日记录', daily_total_count)]
    if 'archive' in visible:
        from archive.models import Archive
        module_chart_items.append(('档案', Archive.objects.count()))
    if 'samples' in visible:
        from samples.models import Sample
        module_chart_items.append(('样品', Sample.objects.filter(is_deleted=False).count()))
    module_chart_items.append(('核价', price_total_count))
    module_chart_items.append(('保险', summary['insurance_total_count']))
    if 'archive' in visible:
        from archive.models import License
        module_chart_items.append(('证照', License.objects.count()))
    module_chart_items.append(('客诉', complaint_total_count))
    module_chart_items.append(('诉讼', lawsuit_total_count))
    module_chart_items.append(('装修', total_decorations))

    # 近 30 天每日记录趋势（按天聚合一次查询，缺数补 0）
    trend_end = timezone.localdate()
    trend_start = trend_end - timedelta(days=29)
    trend_rows = (
        daily_records
        .filter(record_date__date__gte=trend_start, record_date__date__lte=trend_end)
        .annotate(day=TruncDate('record_date'))
        .values('day')
        .annotate(c=Count('id'))
    )
    trend_map = {row['day']: row['c'] for row in trend_rows}
    trend30 = [
        {
            'label': (trend_start + timedelta(days=i)).strftime('%m-%d'),
            'c': trend_map.get(trend_start + timedelta(days=i), 0),
        }
        for i in range(30)
    ]

    chart_payload = {
        'module_labels': [x[0] for x in module_chart_items],
        'module_values': [x[1] for x in module_chart_items],
        'trend_labels': [x['label'] for x in trend30],
        'trend_values': [x['c'] for x in trend30],
    }

    # ===== 试样管理 / 档案管理：数据展示卡片 =====
    from archive.models import Archive, BorrowRecord, LicenseBorrowRecord
    from samples.models import Sample

    sample_qs = Sample.objects.filter(is_deleted=False)
    sample_total_count = sample_qs.count()
    sample_active_count = sample_qs.filter(status__in=[
        Sample.Status.PENDING, Sample.Status.TESTING, Sample.Status.SMALL_BATCH,
    ]).count()
    sample_recent = sample_qs.select_related('supplier').order_by('-registered_at')[:5]

    archive_total_count = Archive.objects.count()
    archive_recent = (
        Archive.objects.select_related('cabinet', 'category').order_by('-created_at')[:5]
    )
    archive_borrowed_count = (
        BorrowRecord.objects.filter(return_time__isnull=True).count()
        + LicenseBorrowRecord.objects.filter(return_time__isnull=True).count()
    )

    module_badges['samples'] = {
        'level': 'warning' if sample_active_count else 'normal',
        'text': f'{sample_active_count} 项进行中' if sample_active_count else '正常',
        'url': '',
    }
    module_badges['archive'] = {
        'level': 'warning' if archive_borrowed_count else 'normal',
        'text': f'{archive_borrowed_count} 份借出中' if archive_borrowed_count else '正常',
        'url': '/archive/borrows/' if archive_borrowed_count else '',
    }

    context = {
        'total_records': summary['total_records'],
        'module_count': summary['module_count'],
        'completion_rate': summary['completion_rate'],
        'pending_count': summary['pending_count'],
        'pending_items': pending_items,  # 返回所有待处理事项
        'daily_pending_count': daily_pending_count,
        'complaint_pending_count': complaint_pending_count,
        'lawsuit_pending_count': lawsuit_pending_count,
        'total_decorations': total_decorations,
        'decoration_reduced_total': summary['decoration_reduced_total'],
        'total_customers': summary['total_customers'],
        'credit_total_count': summary['credit_total_count'],
        'credit_pending_site_count': credit_pending_site_count,
        'credit_agree_count': summary['credit_agree_count'],
        'credit_disagree_count': summary['credit_disagree_count'],
        'complaint_total_count': complaint_total_count,
        'complaint_completed_count': summary['complaint_completed_count'],
        'complaint_incomplete_count': complaint_incomplete_count,
        'lawsuit_total_count': lawsuit_total_count,
        'lawsuit_completed_count': summary['lawsuit_completed_count'],
        'lawsuit_incomplete_count': lawsuit_incomplete_count,
        'total_regulations': summary['total_regulations'],
        'price_total_count': price_total_count,
        'price_reduction_total': summary['price_reduction_total'],
        'insurance_total_count': summary['insurance_total_count'],
        'insurance_not_expired_count': summary['insurance_not_expired_count'],
        'daily_total_count': daily_total_count,
        'daily_incomplete_count': daily_incomplete_count,
        'decoration_list_count': total_decorations,
        'regulation_list_count': summary['total_regulations'],
        'regulation_recent_count': min(5, summary['total_regulations']),
        # 模板仅展示前 5 行，改传切片（无默认排序的模型用 -id，有默认排序的沿用原顺序）
        'decorations': Decoration.objects.order_by('-id')[:5],
        'customer_credits': CustomerCredit.objects.order_by('-id')[:5],
        'customer_complaints': CustomerComplaint.objects.order_by('-id')[:5],
        'complaints': complaints[:5],
        'regulations': Regulation.objects.order_by('-id')[:5],
        'price_evaluations': PriceEvaluation.objects.order_by('-id')[:5],
        'insurances': Insurance.objects.all()[:5],
        'insurance_subjects': insurance_expiring_subjects,
        'daily_records': daily_records[:5],
        'weekly_trend': weekly_trend,
        'trend_max': max([x['c'] for x in weekly_trend] + [1]),
        'module_badges': module_badges,
        'chart_payload': chart_payload,
        'visible_module_keys': visible,
        'sample_total_count': sample_total_count,
        'sample_active_count': sample_active_count,
        'sample_recent': sample_recent,
        'archive_total_count': archive_total_count,
        'archive_borrowed_count': archive_borrowed_count,
        'archive_recent': archive_recent,
    }

    return render(request, 'records/dashboard.html', context)
