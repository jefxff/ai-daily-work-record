"""首页视图（从 records/views.py 拆出，复核报告 ARC-03）。"""

from datetime import timedelta

from django.db.models import Count, F, Q
from django.shortcuts import render
from django.utils import timezone

from accounts.services import visible_modules
from archive.models import Archive
from samples.models import Sample

from . import insurance_grouping, insurance_services
from . import stats as stats_svc
from .models import Complaint, Insurance, Regulation
from .view_common import _my_daily_records, _weekly_trend


def home(request):
    """首页：按用户可见模块裁剪内容。

    改造前首页没有任何权限判定：一个已登录但没有任何模块权限的账号，
    可以直接看到全站数据摘要（审计 #6）。这里不直接加 @perm_required ——
    那会让只有档案权限的用户打不开首页 —— 而是"有哪个模块就统计哪个模块"，
    一个都没有时给出明确空态。
    """
    mods = set(visible_modules(request.user))
    today = timezone.localdate()

    # 先给全部键一个安全的默认值：模板里任何位置引用都不会得到空字符串意外
    context = {
        'mods': mods,
        'no_permission': not mods,
        'today_label': f'{today.year}年{today.month}月{today.day}日 星期{"一二三四五六日"[today.weekday()]}',
        'business_module_count': len(mods & {
            'daily_record', 'customer_credit', 'customer_complaint', 'regulation',
            'price_evaluation', 'decoration', 'insurance', 'complaint', 'samples', 'archive',
        }),
        'focus_tab': next(
            (key for key in ('daily_record', 'insurance', 'complaint') if key in mods), ''
        ),
        'regulation_month_count': 0,
        'regulation_year_count': 0,
        'complaints_closed': 0,
        'total_insurance': 0,
        'insurance_expired_count': 0,
        'total_litigations': 0,
        'litigation_answer_soon_count': 0,
        'total_samples': 0,
        'samples_in_progress': 0,
        'samples_terminated': 0,
        'total_archives': 0,
        'archives_borrowed': 0,
        'licenses_expiring': 0,
        'total_daily_records': 0,
        'incomplete_daily_records_count': 0,
        'incomplete_daily_records': [],
        'total_customers': 0,
        'agree_count': 0,
        'disagree_count': 0,
        'total_complaints': 0,
        'incomplete_complaints': 0,
        'total_regulations': 0,
        'total_price_evaluations': 0,
        'total_price_reduction': 0,
        'total_decorations': 0,
        'decoration_reduced_total': 0,
        'metal_prices': [],
        'metal_price_total': 0,
        'metal_price_updated': None,
        'metal_price_stale': False,
        'insurance_due_soon': [],
        'insurance_due_soon_count': 0,
        'insurance_not_expired_count': 0,
        'insurance_later_count': 0,
        'incomplete_litigations': [],
        'incomplete_litigations_total': 0,
        'todo_counts': None,
        'today_submitted_count': 0,
        'weekly_trend': [],
        'trend_max': 1,
    }
    if not mods:
        return render(request, 'records/home.html', context)

    if 'daily_record' in mods:
        ds = stats_svc.daily_stats(request.user)
        today = timezone.localdate()
        # 首页只渲染最紧急的 5 条（截止日期升序、空值置后），其余到列表页查看
        records = list(
            _my_daily_records(request.user)
            .filter(status='未完成')
            .order_by(F('due_date').asc(nulls_last=True), '-record_date')[:5]
        )
        for rec in records:
            if rec.due_date:
                rec.days_left = (rec.due_date - today).days
                rec.is_overdue = rec.days_left < 0
            else:
                rec.days_left = None
                rec.is_overdue = False
        context.update({
            'total_daily_records': ds['total'],
            'incomplete_daily_records_count': ds['incomplete'],
            'incomplete_daily_records': records,
            'today_submitted_count': _my_daily_records(request.user)
                .filter(record_date__date=today).count(),
            'weekly_trend': _weekly_trend(request.user),
        })
        context['trend_max'] = max([x['c'] for x in context['weekly_trend']] + [1])

    if 'customer_credit' in mods:
        cs = stats_svc.credit_stats()
        context.update({
            'total_customers': cs['distinct_customers'],
            'agree_count': cs['agree'],
            'disagree_count': cs['disagree'],
        })

    if 'customer_complaint' in mods:
        cc = stats_svc.customer_complaint_stats()
        context.update({
            'total_complaints': cc['total'],
            # 「未闭环」= 未完成 + 处理中，与列表页筛选口径一致
            'incomplete_complaints': cc['incomplete'],
            'complaints_closed': cc['total'] - cc['incomplete'],
        })

    if 'regulation' in mods:
        context['total_regulations'] = stats_svc.regulation_count()
        context['regulation_month_count'] = Regulation.objects.filter(
            issue_date__year=today.year, issue_date__month=today.month
        ).count()
        context['regulation_year_count'] = Regulation.objects.filter(
            issue_date__year=today.year
        ).count()

    if 'price_evaluation' in mods:
        ps = stats_svc.price_stats()
        context.update({
            'total_price_evaluations': ps['total_price_evaluations'],
            'total_price_reduction': ps['total_price_reduction'],
        })

    if 'decoration' in mods:
        context.update(stats_svc.decoration_stats())

    if 'metal_price' in mods:
        all_prices = stats_svc.metal_prices_today()
        # 黄金、白银、铜、锡、铝优先展示，其余按名称排序
        priority = ['黄金', '1#白银', '1#铜', '1#锡', 'A00铝']

        def _sort(metal):
            name = metal.metal_name
            return (0, priority.index(name)) if name in priority else (1, name)

        all_prices.sort(key=_sort)
        # 首页只展示前 5 个品种，其余到行情详情页；并给出最后抓取时间与过期提示
        context['metal_prices'] = all_prices[:5]
        context['metal_price_total'] = len(all_prices)
        if all_prices:
            latest = max(p.crawl_time for p in all_prices)
            context['metal_price_updated'] = latest
            context['metal_price_stale'] = latest.date() < timezone.localdate()

    if 'insurance' in mods:
        today = timezone.localdate()
        # 审计 P2-6：仅取聚合所需字段，避免把备注等大字段整表载入
        records = list(Insurance.objects.only(
            "id", "title", "issue_date", "end_date", "premium",
            "company_name", "department",
        ))
        subjects = insurance_grouping.build_subjects(records, today=today)
        summary = insurance_grouping.summarize(subjects)
        expiring_subjects = [s for s in subjects if s.status == 'expiring'][:5]
        for s in expiring_subjects:
            if s.days_left <= 7:
                s.days_level = 'danger'
            else:
                s.days_level = 'warning'
            s.days_text = f'剩 {s.days_left} 天' if s.days_left > 0 else '今天到期'
        expired_subjects_count = sum(
            1 for s in subjects
            if s.status == 'expired' and not insurance_services.one_off_keys().intersection({s.key})
        )
        context['insurance_due_soon'] = expiring_subjects
        context['insurance_due_soon_count'] = summary['expiring']
        context['insurance_not_expired_count'] = summary['active']
        context['insurance_later_count'] = max(0, summary['active'] - summary['expiring'])
        context['total_insurance'] = summary['total']
        context['insurance_expired_count'] = expired_subjects_count

    if 'complaint' in mods:
        today = timezone.localdate()
        open_qs = Complaint.objects.exclude(progress='结案')
        context['total_litigations'] = Complaint.objects.count()
        # 答辩期截止 = 立案 + 15 天；剩余 0~3 天为临期（与数据看板口径一致）
        context['litigation_answer_soon_count'] = open_qs.filter(
            filed_at__range=(today - timedelta(days=15), today - timedelta(days=12))
        ).count()
        # 日期越早越紧急；首页只取最紧急的 5 件
        litigations = list(open_qs.order_by(F('filed_at').asc(nulls_last=True), '-id')[:5])
        for case in litigations:
            case.answer_days = (case.answer_deadline - today).days if case.answer_deadline else None
            if case.answer_days is None:
                case.answer_text, case.answer_level = '未立案', 'muted'
            elif case.answer_days < 0:
                case.answer_text, case.answer_level = f'已过 {abs(case.answer_days)} 天', 'danger'
            else:
                case.answer_text = f'剩 {case.answer_days} 天'
                case.answer_level = 'warning' if case.answer_days <= 3 else 'normal'
        context['incomplete_litigations'] = litigations
        context['incomplete_litigations_total'] = open_qs.count()

    if 'samples' in mods:
        sample_agg = Sample.objects.filter(is_deleted=False).aggregate(
            total=Count('id'),
            in_progress=Count('id', filter=Q(status__in=['pending', 'testing', 'small_batch'])),
            terminated=Count('id', filter=Q(status='terminated')),
        )
        context.update({
            'total_samples': sample_agg['total'],
            'samples_in_progress': sample_agg['in_progress'],
            'samples_terminated': sample_agg['terminated'],
        })

    if 'archive' in mods:
        from archive.models import BorrowRecord, License, LicenseBorrowRecord
        context.update({
            'total_archives': Archive.objects.count(),
            'archives_borrowed': (
                BorrowRecord.objects.filter(return_time__isnull=True).count()
                + LicenseBorrowRecord.objects.filter(return_time__isnull=True).count()
            ),
            # 与证照「即将到期」判定一致：有效期或下次年审日在 30 天内
            'licenses_expiring': License.objects.filter(
                Q(expire_date__gte=today, expire_date__lte=today + timedelta(days=30))
                | Q(next_review_date__gte=today, next_review_date__lte=today + timedelta(days=30))
            ).count(),
        })

    # 行动区：跨模块待办计数与工作台同一口径（views_workspace.collect_todos）
    if mods & {'daily_record', 'customer_complaint', 'complaint', 'samples', 'archive'}:
        from .views_workspace import todo_counts
        context['todo_counts'] = todo_counts(request.user)

    return render(request, 'records/home.html', context)
