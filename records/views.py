from django.shortcuts import render, get_object_or_404, redirect
from django.http import HttpResponse, JsonResponse, HttpResponseBadRequest
from django.views.decorators.http import require_POST
from django.template.loader import render_to_string
from django.db.models import Count, F, Q
from django.db.models.functions import TruncDate
from datetime import date, datetime, timedelta
from .models import Decoration, CustomerCredit, CustomerComplaint, Regulation, PriceEvaluation, DailyRecord, MetalPrice, Insurance, Complaint, OperationLog
from .forms import DecorationForm, CustomerCreditForm, CustomerComplaintForm, CustomerComplaintClosureForm, RegulationForm, PriceEvaluationForm, InsuranceForm, ComplaintForm
from .audit import log_operation, snapshot
from .crud import make_delete_view, make_export_view, make_form_view, make_list_view
from django.utils import timezone
from accounts.decorators import perm_required, require_any_module
from accounts.services import visible_modules
from knowledge_base.models import KnowledgeDocument
from samples.models import Sample, Partner
from archive.models import Archive

import logging

logger = logging.getLogger(__name__)


def _is_ajax(request):
    return request.headers.get('x-requested-with') == 'XMLHttpRequest'


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


def home(request):
    """首页：按用户可见模块裁剪内容。

    改造前首页没有任何权限判定：一个已登录但没有任何模块权限的账号，
    可以直接看到全站数据摘要（审计 #6）。这里不直接加 @perm_required ——
    那会让只有档案权限的用户打不开首页 —— 而是"有哪个模块就统计哪个模块"，
    一个都没有时给出明确空态。
    """
    from . import stats as stats_svc

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
        soon_qs = Insurance.objects.filter(
            end_date__gte=today, end_date__lte=today + timedelta(days=30)
        ).order_by('end_date')
        agg = Insurance.objects.aggregate(
            total=Count('id'),
            not_expired=Count('id', filter=Q(end_date__gt=today)),
            due_soon=Count('id', filter=Q(end_date__gte=today, end_date__lte=today + timedelta(days=30))),
            expired=Count('id', filter=Q(end_date__lt=today)),
        )
        soon = list(soon_qs[:5])
        for ins in soon:
            ins.days_left = (ins.end_date - today).days
            if ins.days_left <= 7:
                ins.days_level = 'danger'
            elif ins.days_left <= 30:
                ins.days_level = 'warning'
            else:
                ins.days_level = 'normal'
            ins.days_text = f'剩 {ins.days_left} 天' if ins.days_left > 0 else '今天到期'
        context['insurance_due_soon'] = soon
        context['insurance_due_soon_count'] = agg['due_soon']
        context['insurance_not_expired_count'] = agg['not_expired']
        context['insurance_later_count'] = agg['not_expired'] - agg['due_soon']
        context['total_insurance'] = agg['total']
        context['insurance_expired_count'] = agg['expired']

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


PER_GROUP_CAP = 50


@require_any_module
def search(request):
    """全站搜索。

    三处改造（审计 #3 / #6）：
    1. 空关键词直接返回空态：改造前 icontains('') 会匹配全表，13 个模型各发一条
       无 LIMIT 的 SELECT（实测 487 SQL / 1456ms）；
    2. 每组硬顶 50 条并给出真实条数：搜索页的语义是"按关键词定位"，
       命中过多时应提示补充关键词，而不是让用户翻 20 页；
    3. 只查用户有 read 权限的模块（原实现查完再置空，SQL 已经发出去了）。
    """
    search_term = (request.GET.get('search') or '').strip()
    visible = set(visible_modules(request.user))

    if not search_term:
        return render(request, 'records/search_results.html', {
            'empty_term': True,
            'search_term': '',
            'search_counts': {},
            'search_total': 0,
        })
    
    # 搜索装修记录
    decoration_results = Decoration.objects.filter(
        Q(project_name__icontains=search_term) |
        Q(constructor__icontains=search_term) |
        Q(project_address__icontains=search_term)
    )
    
    # 搜索客户账期
    customer_credit_results = CustomerCredit.objects.filter(
        Q(customer_name__icontains=search_term) |
        Q(salesperson__icontains=search_term) |
        Q(work_content__icontains=search_term) |
        Q(specific_content__icontains=search_term)
    )
    
    # 搜索客户客诉
    customer_complaint_results = CustomerComplaint.objects.filter(
        Q(customer_name__icontains=search_term) |
        Q(responsible_salesperson__icontains=search_term) |
        Q(terminal_customer__icontains=search_term) |
        Q(product_model__icontains=search_term) |
        Q(complaint_description__icontains=search_term) |
        Q(engineering_report__icontains=search_term) |
        Q(engineering_analysis__icontains=search_term) |
        Q(missing_materials__icontains=search_term) |
        Q(follow_up_status__icontains=search_term) |
        Q(remarks__icontains=search_term)
    )
    
    # 搜索制度及处罚
    regulation_results = Regulation.objects.filter(
        Q(title__icontains=search_term) |
        Q(content__icontains=search_term) |
        Q(doc_no__icontains=search_term)
    )
    
    # 搜索核价
    price_evaluation_results = PriceEvaluation.objects.filter(
        Q(project_category__icontains=search_term) |
        Q(proposing_department__icontains=search_term) |
        Q(proposer__icontains=search_term) |
        Q(evaluation_content__icontains=search_term)
    )
    
    # 搜索每日记录（仅当前用户的记录）
    daily_record_results = _my_daily_records(request.user).filter(
        Q(department__icontains=search_term) |
        Q(work_content__icontains=search_term) |
        Q(status__icontains=search_term)
    )
    
    # 搜索保险记录
    insurance_results = Insurance.objects.filter(
        Q(company_name__icontains=search_term) |
        Q(title__icontains=search_term) |
        Q(order_number__icontains=search_term) |
        Q(department__icontains=search_term) |
        Q(remarks__icontains=search_term)
    )
    
    # 搜索金属价格记录
    metal_price_results = MetalPrice.objects.filter(
        Q(metal_name__icontains=search_term)
    )
    
    # 搜索客诉记录
    complaint_results = Complaint.objects.filter(
        Q(dispute_type__icontains=search_term) |
        Q(defendant__icontains=search_term) |
        Q(case_number__icontains=search_term) |
        Q(court__icontains=search_term) |
        Q(lawyer__icontains=search_term) |
        Q(progress__icontains=search_term) |
        Q(detailed_progress__icontains=search_term) |
        Q(remarks__icontains=search_term)
    )

    # 搜索知识库文档（与列表行为一致，仅覆盖已发布文档；
    # 可见性过滤统一走 knowledge_base.access 唯一入口，防止他人私有文档进搜索结果）
    from knowledge_base import access as kb_access
    knowledge_results = kb_access.visible_docs(
        request.user, KnowledgeDocument.objects.select_related('category')
    ).filter(status='published').filter(
        Q(title__icontains=search_term) |
        Q(content__icontains=search_term) |
        Q(summary__icontains=search_term) |
        Q(tags__icontains=search_term)
    )

    # 搜索样品主档（排除软删除，含供应商名称）
    sample_results = Sample.objects.select_related('supplier').filter(is_deleted=False).filter(
        Q(sample_no__icontains=search_term) |
        Q(material_name__icontains=search_term) |
        Q(model__icontains=search_term) |
        Q(spec__icontains=search_term) |
        Q(registrar__icontains=search_term) |
        Q(supplier__name__icontains=search_term)
    )

    # 搜索供应商档案（试样管理模块）
    partner_results = Partner.objects.filter(
        Q(name__icontains=search_term) |
        Q(partner_code__icontains=search_term) |
        Q(contact_person__icontains=search_term) |
        Q(contact_info__icontains=search_term)
    )

    # 搜索档案（档案管理模块，含台账名称与类别名称）
    archive_results = Archive.objects.select_related(
        'ledger', 'category', 'cabinet'
    ).filter(
        Q(code__icontains=search_term) |
        Q(name__icontains=search_term) |
        Q(description__icontains=search_term) |
        Q(tags__icontains=search_term) |
        Q(location_code__icontains=search_term) |
        Q(counterpart__icontains=search_term) |
        Q(contract_no__icontains=search_term) |
        Q(department__icontains=search_term) |
        Q(handler__icontains=search_term) |
        Q(ledger__name__icontains=search_term) |
        Q(category__name__icontains=search_term)
    )

    # 按用户可见模块收敛搜索结果：无权限的模块既不查计数也不查数据
    groups = {
        'decoration': ('decoration_results', decoration_results),
        'customer_credit': ('customer_credit_results', customer_credit_results),
        'customer_complaint': ('customer_complaint_results', customer_complaint_results),
        'regulation': ('regulation_results', regulation_results),
        'price_evaluation': ('price_evaluation_results', price_evaluation_results),
        'daily_record': ('daily_record_results', daily_record_results),
        'insurance': ('insurance_results', insurance_results),
        'metal_price': ('metal_price_results', metal_price_results),
        'complaint': ('complaint_results', complaint_results),
        'knowledge': ('knowledge_results', knowledge_results),
        'samples': ('sample_results', sample_results),
        'partners': ('partner_results', partner_results),
        'archive': ('archive_results', archive_results),
    }
    # 分组 key 多为权限模块 key；供应商档案归属 samples 模块（评审 R13(b)：
    # 原先直接拿 'partners' 去比对 visible_modules，而 registry 中并无 'partners'，
    # 导致拥有 samples 权限的用户搜索"供应商"结果永远为空）。
    perm_of = {key: key for key in groups}
    perm_of['partners'] = 'samples'
    context = {'search_term': search_term, 'empty_term': False}
    counts = {}
    for key, (var, qs) in groups.items():
        if perm_of[key] in visible:
            counts[key] = qs.count()
            context[var] = qs[:PER_GROUP_CAP]
        else:
            counts[key] = 0
            context[var] = []
    context['search_counts'] = counts
    context['search_total'] = sum(counts.values())
    context['per_group_cap'] = PER_GROUP_CAP
    return render(request, 'records/search_results.html', context)


# 装修记录视图
decoration_list = make_list_view(
    perm_module='decoration', template_name='records/decoration_list.html',
    page_var='decorations', model=Decoration,
    search_fields=['project_name', 'constructor', 'project_address'],
    filters=[('constructor', 'constructor')],
    filter_options={'constructors': 'constructor'},
)

# 新增/编辑视图（工厂统一实现：权限校验 + AJAX 弹窗双模式 + 操作留痕）
decoration_add, decoration_edit = make_form_view(
    model=Decoration, form_class=DecorationForm, perm_module='decoration',
    log_label='装修', summary_label='装修记录', display_field='project_name',
    redirect_name='decoration_list', template_name='records/decoration_form.html',
    title_add='新增装修记录', title_edit='编辑装修记录',
)

decoration_export = make_export_view(
    model=Decoration, perm_module='decoration', sheet_title='装修记录', log_label='装修',
    filename_base='装修记录', empty_text='当前无装修记录，无法导出Excel',
    headers=['项目名称', '施工方', '项目地址', '开工日期', '原价', '核后价', '核减金额'],
    row_values=lambda obj, serial: [
        obj.project_name, obj.constructor, obj.project_address or '',
        obj.start_date.strftime('%Y-%m-%d') if obj.start_date else '',
        obj.original_price or 0, obj.final_price or 0, obj.reduced_price or 0,
    ],
)


# 客户账期视图
customer_credit_list = make_list_view(
    perm_module='customer_credit', template_name='records/customer_credit_list.html',
    page_var='credits', model=CustomerCredit,
    search_fields=['customer_name', 'salesperson', 'work_content'],
    filters=[
        ('customer_name', 'customer_name__icontains'),
        ('customer_type', 'customer_type'),
        ('salesperson', 'salesperson'),
        ('specific_content', 'specific_content'),
        ('work_method', 'work_method'),
    ],
    filter_options={'salespersons': 'salesperson'},
    extra_context={
        'specific_content_choices': [('老客户重审', '老客户重审'), ('提升账期或额度', '提升账期或额度'), ('新客户账期', '新客户账期')],
        'work_method_choices': [('刘总特批', '刘总特批'), ('现场审核', '现场审核'), ('内部流程', '内部流程')],
        'customer_type_choices': [('普通客户', '普通客户'), ('代加工客户', '代加工客户')],
    },
)

customer_credit_add, customer_credit_edit = make_form_view(
    model=CustomerCredit, form_class=CustomerCreditForm, perm_module='customer_credit',
    log_label='客户账期', display_field='customer_name',
    redirect_name='customer_credit_list', template_name='records/customer_credit_form.html',
    title_add='新增客户账期', title_edit='编辑客户账期',
)

customer_credit_export = make_export_view(
    model=CustomerCredit, perm_module='customer_credit', sheet_title='客户账期', log_label='客户账期',
    filename_base='客户账期', empty_text='当前无客户账期记录，无法导出Excel',
    headers=['提出日期', '现场日期', '客户名称', '客户类型', '业务员', '工作内容', '具体内容', '前期调查表', '工作方式', '结果', '备注', '是否有业绩考核期', '考核期开始日期', '考核期结束日期', '考核内容'],
    row_values=lambda obj, serial: [
        obj.proposal_date.strftime('%Y-%m-%d') if obj.proposal_date else '',
        obj.site_date.strftime('%Y-%m-%d') if obj.site_date else '',
        obj.customer_name, obj.customer_type, obj.salesperson,
        obj.work_content or '', obj.specific_content or '', obj.pre_survey or '',
        obj.work_method or '', obj.result, obj.remarks or '',
        '是' if obj.has_assessment_period else '否',
        obj.assessment_start_date.strftime('%Y-%m-%d') if obj.assessment_start_date else '',
        obj.assessment_end_date.strftime('%Y-%m-%d') if obj.assessment_end_date else '',
        obj.assessment_content or '',
    ],
)


# 客户客诉视图
customer_complaint_list = make_list_view(
    perm_module='customer_complaint', template_name='records/customer_complaint_list.html',
    page_var='complaints', model=CustomerComplaint,
    search_fields=['customer_name', 'responsible_salesperson', 'product_model', 'complaint_description'],
    filters=[
        ('customer_name', 'customer_name__icontains'),
        ('responsible_salesperson', 'responsible_salesperson'),
        ('product_model', 'product_model'),
        ('is_insured', 'is_insured', lambda v: v == 'true'),
        ('complaint_status', 'complaint_status'),
    ],
    filter_options={'responsible_salespersons': 'responsible_salesperson', 'product_models': 'product_model'},
    extra_context={'complaint_status_choices': [('未完成', '未完成'), ('处理中', '处理中'), ('已完成', '已完成')]},
)

customer_complaint_add, customer_complaint_edit = make_form_view(
    model=CustomerComplaint, form_class=CustomerComplaintForm, perm_module='customer_complaint',
    log_label='客户客诉', display_field='customer_name',
    redirect_name='customer_complaint_list', template_name='records/customer_complaint_form.html',
    title_add='新增客户客诉', title_edit='编辑客户客诉',
)

customer_complaint_export = make_export_view(
    model=CustomerComplaint, perm_module='customer_complaint', sheet_title='客户客诉', log_label='客户客诉',
    filename_base='客户客诉', empty_text='当前无客户客诉记录，无法导出Excel',
    headers=['客户名称', '责任业务员', '客诉申请日期', '终端客户', '产品型号', '我司出货量', '客户出货量', '客诉现象及客户诉求描述', '客户诉求（赔偿金额）', '是否报保险', '工程报告', '工程部分析原因报告', '欠缺材料', '客诉状态', '跟进情况', '闭环结论', '闭环时间', '闭环人', '备注'],
    row_values=lambda obj, serial: [
        obj.customer_name, obj.responsible_salesperson,
        obj.complaint_date.strftime('%Y-%m-%d') if obj.complaint_date else '',
        obj.terminal_customer or '', obj.product_model or '',
        obj.our_shipment or 0, obj.customer_shipment or 0,
        obj.complaint_description or '', obj.compensation_amount or 0,
        '是' if obj.is_insured else '否', obj.engineering_report or '',
        obj.engineering_analysis or '', obj.missing_materials or '',
        obj.complaint_status, obj.follow_up_status or '',
        obj.closure_result_display,
        obj.closed_at.strftime('%Y-%m-%d %H:%M') if obj.closed_at else '',
        str(obj.closed_by) if obj.closed_by else '',
        obj.remarks or '',
    ],
)


@perm_required("customer_complaint", "read")
def customer_detail(request, pk):
    """客户详情视图（已取消 AJAX 弹窗，一律整页渲染）"""
    # 获取客户客诉信息
    complaint = get_object_or_404(CustomerComplaint, pk=pk)

    # 可以在这里添加其他相关信息的查询，比如该客户的其他客诉记录等
    other_complaints = CustomerComplaint.objects.filter(customer_name=complaint.customer_name).exclude(id=pk)

    context = {
        'complaint': complaint,
        'other_complaints': other_complaints
    }
    return render(request, 'records/customer_detail.html', context)


@perm_required("customer_complaint", "update")
@require_POST
def customer_complaint_close(request, pk):
    """客诉闭环操作：写入闭环结论/说明，自动回填闭环时间与闭环人。

    闭环动作同步将 complaint_status 置为「已完成」、follow_up_status 置为「已闭环」，
    并记录操作留痕、触发闭环通知（通知失败不影响主流程）。
    """
    complaint = get_object_or_404(CustomerComplaint, pk=pk)
    form = CustomerComplaintClosureForm(request.POST)
    if not form.is_valid():
        return redirect('customer_detail', pk=pk)

    before_snap = snapshot(complaint)
    complaint.closure_result = form.cleaned_data['closure_result']
    complaint.closure_note = form.cleaned_data['closure_note']
    complaint.closed_at = timezone.now()
    complaint.closed_by = request.user
    # 状态由闭环动作推导：闭环即已完成
    complaint.complaint_status = '已完成'
    complaint.follow_up_status = '已闭环'
    complaint.save()
    log_operation(
        request, '客户客诉', '状态变更', f'[客户客诉]下的记录#{complaint.id}',
        f'客诉闭环: {complaint.customer_name}（结论：{complaint.get_closure_result_display()}）',
        before=before_snap, after=snapshot(complaint),
    )

    # 通知接入：闭环告知登记人与责任业务员（业务员为文本字段，无法精确匹配用户，
    # 故仅通知当前操作人；若后续业务员关联用户表可扩展收件人）
    try:
        from notifications.services import emit
        emit('complaint_closed', recipient=request.user,
             context={
                 'customer_name': complaint.customer_name,
                 'closed_at': timezone.localtime(complaint.closed_at).strftime('%Y-%m-%d %H:%M'),
                 'closure_result': complaint.get_closure_result_display(),
                 'closed_by': request.user.get_username(),
                 'closure_note': complaint.closure_note or '',
             },
             link=f'/customer_detail/{complaint.pk}/')
    except Exception:
        logger.exception("客诉闭环通知发送失败")

    return redirect('customer_detail', pk=pk)


# 制度及处罚视图
regulation_list = make_list_view(
    perm_module='regulation', template_name='records/regulation_list.html',
    page_var='regulations', model=Regulation,
    search_fields=['title', 'content', 'doc_no'],
)

regulation_add, regulation_edit = make_form_view(
    model=Regulation, form_class=RegulationForm, perm_module='regulation',
    log_label='制度及处罚', display_field='title',
    redirect_name='regulation_list', template_name='records/regulation_form.html',
    title_add='新增制度及处罚', title_edit='编辑制度及处罚',
)

regulation_export = make_export_view(
    model=Regulation, perm_module='regulation', sheet_title='制度及处罚', log_label='制度及处罚',
    filename_base='制度及处罚', empty_text='当前无制度及处罚记录，无法导出Excel', column_width=30,
    headers=['出文日期', '标题', '主要内容', '文件编号'],
    row_values=lambda obj, serial: [
        obj.issue_date.strftime('%Y-%m-%d') if obj.issue_date else '',
        obj.title, obj.content or '', obj.doc_no,
    ],
)


@perm_required("regulation", "read")
def regulation_detail(request, pk):
    """制度详情视图"""
    # 获取制度信息
    regulation = get_object_or_404(Regulation, pk=pk)
    context = {'regulation': regulation}
    if _is_ajax(request):
        return _modal_detail(request, 'records/_regulation_detail_modal.html', context)
    return render(request, 'records/regulation_detail.html', context)


# 核价视图
price_evaluation_list = make_list_view(
    perm_module='price_evaluation', template_name='records/price_evaluation_list.html',
    page_var='evaluations', model=PriceEvaluation,
    search_fields=['project_category', 'proposing_department', 'proposer', 'evaluation_content'],
)

# 核价留痕摘要展示 project_category（原实现误引不存在的 project_name，提交后 500，已顺手修正）
price_evaluation_add, price_evaluation_edit = make_form_view(
    model=PriceEvaluation, form_class=PriceEvaluationForm, perm_module='price_evaluation',
    log_label='核价', display_field='project_category',
    redirect_name='price_evaluation_list', template_name='records/price_evaluation_form.html',
    title_add='新增核价', title_edit='编辑核价',
)

price_evaluation_export = make_export_view(
    model=PriceEvaluation, perm_module='price_evaluation', sheet_title='核价', log_label='核价',
    filename_base='核价', empty_text='当前无核价记录，无法导出Excel',
    headers=['项目分类', '项目提出部门', '提出人', '核价内容', '供应商1名称', '供应商1报价', '供应商2名称', '供应商2报价', '供应商3名称', '供应商3报价', '内控审核供应商', '内控审核价'],
    row_values=lambda obj, serial: [
        obj.project_category, obj.proposing_department, obj.proposer,
        obj.evaluation_content or '', obj.supplier1_name or '', obj.supplier1_price or 0,
        obj.supplier2_name or '', obj.supplier2_price or 0,
        obj.supplier3_name or '', obj.supplier3_price or 0,
        obj.internal_control_supplier or '', obj.internal_control_price,
    ],
)


# 每日记录视图
@perm_required("daily_record", "create")
def daily_record_add(request):
    """新增每日记录视图"""
    from django.http import JsonResponse
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    if request.method == 'POST':
        # 手动处理表单数据
        department = (request.POST.get('department') or '').strip()
        work_content = (request.POST.get('work_content') or '').strip()
        status = request.POST.get('status') or '未完成'
        progress_raw = request.POST.get('progress', '0') or '0'
        due_date_raw = (request.POST.get('due_date') or '').strip()

        # 校验
        errors = {}
        if not department:
            errors['department'] = '请填写提出部门'
        if not work_content:
            errors['work_content'] = '请填写工作内容'
        try:
            progress = int(progress_raw)
            if progress < 0 or progress > 100:
                errors['progress'] = '工作进度必须在 0-100 之间'
        except (TypeError, ValueError):
            errors['progress'] = '工作进度必须为数字'
            progress = 0
        if status not in ('完成', '未完成'):
            errors['status'] = '工作状态非法'
        # 截至日期：选填，格式 YYYY-MM-DD
        due_date = None
        if due_date_raw:
            try:
                due_date = datetime.strptime(due_date_raw, '%Y-%m-%d').date()
            except ValueError:
                errors['due_date'] = '截至日期格式不正确'

        if errors:
            if is_ajax:
                from django.template.loader import render_to_string
                html = render_to_string('records/daily_record_form_modal.html', {
                    'record': None,
                    'now': timezone.now(),
                    'errors': errors,
                    'form_data': {'department': department, 'work_content': work_content, 'status': status, 'progress': progress_raw, 'due_date': due_date_raw},
                }, request=request)
                return JsonResponse({'ok': False, 'html': html}, status=422)
            return render(request, 'records/daily_record_form.html', {'errors': errors})

        # 创建并保存记录（归属绑定当前用户，服务端强制，忽略前端提交的归属字段）
        record = DailyRecord.objects.create(
            user=request.user,
            department=department,
            work_content=work_content,
            due_date=due_date,
            status=status,
            progress=progress
        )
        log_operation(request, '每日记录', '新增', '[每日记录]下的新记录',
                      f'新增每日记录: {work_content}', after=snapshot(record))
        # 知识库沉淀（批次 3）：表单两问（要不要沉淀/给谁看）；失败只记日志不影响主流程
        from records import kb_sync as kb_service
        kb_service.apply_flag(
            record,
            enabled=request.POST.get('kb_enabled') == 'on',
            visibility=request.POST.get('kb_visibility') or 'private',
            actor=request.user, request=request)
        # 通知触发：保存为「未完成」时提醒本人（同记录幂等，编辑改回未完成不会重复提醒）
        if status == '未完成':
            try:
                from notifications.services import emit
                emit('daily_record_incomplete', recipient=request.user,
                     context={'record_date': timezone.now(), 'today': timezone.localdate(),
                              'progress': progress, 'work_content': work_content[:50]},
                     dedup_context=f'incomplete-new:{request.user.pk}:{timezone.localdate()}',
                     link='/daily_record/')
            except Exception:
                # 通知失败不影响业务主流程，但必须留痕以便事后排查
                logger.exception("每日记录新增后触发「未完成」通知失败")
        if is_ajax:
            return JsonResponse({'ok': True, 'message': '新增成功'})
        return redirect('home')
    if is_ajax:
        from django.template.loader import render_to_string
        html = render_to_string('records/daily_record_form_modal.html', {
            'record': None,
            'now': timezone.now(),
            'errors': {},
            'form_data': {},
        }, request=request)
        return JsonResponse({'ok': True, 'html': html})
    return render(request, 'records/daily_record_form.html')

daily_record_list = make_list_view(
    perm_module='daily_record', template_name='records/daily_record_list.html',
    page_var='records',
    queryset=lambda request: _my_daily_records(request.user).order_by('-record_date'),
    search_fields=['work_content', 'department'],
    filters=[('department', 'department'), ('status', 'status')],
    option_queries={'departments': ('department', lambda request: _my_daily_records(request.user))},
    extra_context={'status_choices': [('完成', '完成'), ('未完成', '未完成')]},
)

@perm_required("daily_record", "update")
def daily_record_edit(request, pk):
    """编辑每日记录视图"""
    from django.http import JsonResponse
    record = _get_my_record(request, pk)
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    if request.method == 'POST':
        department = (request.POST.get('department') or '').strip()
        work_content = (request.POST.get('work_content') or '').strip()
        status = request.POST.get('status') or '未完成'
        progress_raw = request.POST.get('progress', '0') or '0'
        due_date_raw = (request.POST.get('due_date') or '').strip()

        errors = {}
        if not department:
            errors['department'] = '请填写提出部门'
        if not work_content:
            errors['work_content'] = '请填写工作内容'
        try:
            progress = int(progress_raw)
            if progress < 0 or progress > 100:
                errors['progress'] = '工作进度必须在 0-100 之间'
        except (TypeError, ValueError):
            errors['progress'] = '工作进度必须为数字'
            progress = record.progress
        if status not in ('完成', '未完成'):
            errors['status'] = '工作状态非法'
        # 截至日期：选填，格式 YYYY-MM-DD（留空表示清除）
        due_date = None
        if due_date_raw:
            try:
                due_date = datetime.strptime(due_date_raw, '%Y-%m-%d').date()
            except ValueError:
                errors['due_date'] = '截至日期格式不正确'

        if errors:
            if is_ajax:
                # 写回用户输入的值（不持久化），重渲染局部模板
                record.department = department
                record.work_content = work_content
                record.status = status
                if 'due_date' not in errors:
                    record.due_date = due_date
                from django.template.loader import render_to_string
                html = render_to_string('records/daily_record_form_modal.html', {
                    'record': record,
                    'errors': errors,
                }, request=request)
                return JsonResponse({'ok': False, 'html': html}, status=422)
            return render(request, 'records/daily_record_form.html', {'record': record, 'errors': errors})

        # 写入
        before_snap = snapshot(record)
        record.department = department
        record.work_content = work_content
        record.status = status
        record.progress = progress
        record.due_date = due_date
        record.save()
        log_operation(request, '每日记录', '更新', f'[每日记录]下的记录#{record.id}',
                      f'编辑每日记录: {record.work_content}',
                      before=before_snap, after=snapshot(record))
        # 知识库沉淀（批次 3）：开关/范围有变化时同步；失败只记日志不影响主流程
        from records import kb_sync as kb_service
        kb_service.apply_flag(
            record,
            enabled=request.POST.get('kb_enabled') == 'on',
            visibility=request.POST.get('kb_visibility') or 'private',
            actor=request.user, request=request)
        if is_ajax:
            return JsonResponse({'ok': True, 'message': '保存成功'})
        return redirect('daily_record_list')
    if is_ajax:
        from django.template.loader import render_to_string
        html = render_to_string('records/daily_record_form_modal.html', {
            'record': record,
            'errors': {},
        }, request=request)
        return JsonResponse({'ok': True, 'html': html})
    return render(request, 'records/daily_record_form.html', {'record': record})

@perm_required("daily_record", "read")
def daily_record_detail(request, pk):
    """每日记录详情视图"""
    record = _get_my_record(request, pk)
    if _is_ajax(request):
        return _modal_detail(request, 'records/_daily_record_detail_modal.html', {'record': record})
    return render(request, 'records/daily_record_detail.html', {'record': record})


@perm_required("daily_record", "create")
def daily_record_kb_sync_batch(request):
    """批量沉淀：当前用户全部未沉淀记录 → 知识库（一律私有，D5 拍板）。

    独立于单条表单开关：给「先记了、后来想归档」的人一个一次到位的入口。
    """
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'message': '仅支持 POST 请求'}, status=405)
    from records import kb_sync as kb_service
    ok, fail = kb_service.sync_batch(request.user, request=request)
    if ok or fail:
        log_operation(request, '每日记录', '批量沉淀', '[每日记录]下的批量沉淀',
                      f'批量沉淀每日记录到知识库：成功 {ok} 条，失败 {fail} 条')
    message = f'批量沉淀完成：成功 {ok} 条' + (f'，失败 {fail} 条' if fail else '，没有需要沉淀的记录')
    return JsonResponse({'ok': True, 'message': message, 'synced': ok, 'failed': fail})

daily_record_export = make_export_view(
    perm_module='daily_record', sheet_title='每日记录', log_label='每日记录',
    filename_base='每日记录', empty_text='当前无每日记录，无法导出Excel',
    queryset=lambda request: _my_daily_records(request.user).order_by('-record_date'),
    headers=['序号', '日期及时间', '提出部门', '工作内容', '工作状态', '工作进度'],
    row_values=lambda obj, serial: [
        serial, obj.record_date.strftime('%Y-%m-%d %H:%M:%S'),
        obj.department, obj.work_content, obj.status, f'{obj.progress}%',
    ],
)


# 删除功能视图（工厂统一实现：仅 POST + 权限校验 + 操作留痕）
decoration_delete = make_delete_view(
    model=Decoration, perm_module='decoration', label='装修',
    display_field='project_name', redirect_name='decoration_list',
)

customer_credit_delete = make_delete_view(
    model=CustomerCredit, perm_module='customer_credit', label='客户账期',
    display_field='customer_name', redirect_name='customer_credit_list',
)

customer_complaint_delete = make_delete_view(
    model=CustomerComplaint, perm_module='customer_complaint', label='客户客诉',
    display_field='customer_name', redirect_name='customer_complaint_list',
)

regulation_delete = make_delete_view(
    model=Regulation, perm_module='regulation', label='制度及处罚',
    display_field='title', redirect_name='regulation_list',
)

price_evaluation_delete = make_delete_view(
    model=PriceEvaluation, perm_module='price_evaluation', label='核价',
    display_field='project_category', redirect_name='price_evaluation_list',
)

daily_record_delete = make_delete_view(
    perm_module='daily_record', label='每日记录',
    display_field='work_content', redirect_name='daily_record_list',
    getter=_get_my_record,
)


# 保险视图
def _filter_insurance_expired(qs, value):
    """保险到期状态筛选：expired=已到期，not_expired=未到期（与原实现一致）。"""
    today = date.today()
    if value == 'expired':
        return qs.filter(end_date__lt=today)
    if value == 'not_expired':
        return qs.filter(end_date__gte=today)
    return qs


insurance_list = make_list_view(
    perm_module='insurance', template_name='records/insurance_list.html',
    page_var='insurances', model=Insurance,
    search_fields=['company_name', 'title', 'order_number', 'department', 'remarks'],
    filters=[
        ('company_name', 'company_name__icontains'),
        ('title', 'title__icontains'),
        ('department', 'department'),
        ('is_expired', _filter_insurance_expired),
        ('date_start', 'end_date__gte', None, 'date_start'),
        ('date_end', 'end_date__lte', None, 'date_end'),
    ],
    filter_options={'departments': 'department'},
)

insurance_add, insurance_edit = make_form_view(
    model=Insurance, form_class=InsuranceForm, perm_module='insurance',
    log_label='保险', display_field='company_name',
    redirect_name='insurance_list', template_name='records/insurance_form.html',
    title_add='新增保险', title_edit='编辑保险',
)

insurance_delete = make_delete_view(
    model=Insurance, perm_module='insurance', label='保险',
    display_field='company_name', redirect_name='insurance_list',
)

insurance_export = make_export_view(
    model=Insurance, perm_module='insurance', sheet_title='保险记录', log_label='保险',
    filename_base='保险记录', empty_text='当前无保险记录，无法导出Excel',
    headers=['序号', '公司名称', '标题', '单号', '保费', '签发日期', '截止日期', '是否已经到期', '部门', '备注'],
    row_values=lambda obj, serial: [
        serial, obj.company_name, obj.title, obj.order_number or '',
        obj.premium, obj.issue_date.strftime('%Y-%m-%d'), obj.end_date.strftime('%Y-%m-%d'),
        obj.expired_days, obj.department, obj.remarks or '',
    ],
)


# 金属价格详情视图
@perm_required("metal_price", "read")
def metal_price_detail(request, metal_name):
    """金属价格详情视图 - 优化数据库查询"""
    # 优化查询：一次性获取所有需要的数据
    
    # 获取所有金属名称列表（去重）
    all_metals = sorted(list(set(MetalPrice.objects.values_list('metal_name', flat=True))))
    
    # 优化：一次性获取当前金属的所有相关数据
    metal_prices = MetalPrice.objects.filter(metal_name=metal_name).order_by('-crawl_date')
    
    # 获取当前金属的最新价格
    latest_price = metal_prices.first()
    
    # 获取该金属的历史价格（最近30天）
    price_history = list(metal_prices[:30])
    
    # 图表数据使用相同的数据集，避免重复查询
    chart_labels = []
    chart_data = []
    
    # 修复：反转数据以正确的时间顺序显示图表
    for price in reversed(price_history):
        chart_labels.append(price.crawl_date.strftime('%m-%d'))
        chart_data.append(float(price.price))
    
    context = {
        'current_metal': metal_name,
        'all_metals': all_metals,
        'latest_price': latest_price,
        'price_history': price_history,
        'chart_labels': chart_labels,
        'chart_data': chart_data,
    }
    if _is_ajax(request):
        return _modal_detail(request, 'records/_metal_price_detail_modal.html', context)
    return render(request, 'records/metal_price_detail.html', context)


@perm_required("metal_price", "update")
def refresh_metal_prices(request):
    """刷新金属价格数据"""
    from django.http import JsonResponse
    from .metal_price_service import fetch_and_save_prices

    try:
        saved_prices = fetch_and_save_prices(use_sample=False)

        if saved_prices:
            metals = '、'.join([p.metal_name for p in saved_prices])
            log_operation(request, '金属价格', '更新', '[金属价格]下的价格刷新',
                          f'刷新金属价格: {metals}（共{len(saved_prices)}种）')
            return JsonResponse({
                'success': True,
                'message': f'成功更新 {len(saved_prices)} 种金属价格',
                'count': len(saved_prices)
            })
        else:
            log_operation(request, '金属价格', '更新', '[金属价格]下的价格刷新', '刷新金属价格: 未能获取到数据')
            return JsonResponse({
                'success': False,
                'message': '未能获取到金属价格数据'
            })
    except Exception as e:
        log_operation(request, '金属价格', '更新', '[金属价格]下的价格刷新', f'刷新金属价格出错: {str(e)}')
        return JsonResponse({
            'success': False,
            'message': f'更新价格时出错: {str(e)}'
        })


@perm_required("data_view", "read")
def dashboard(request):
    """数据展示看板视图"""
    from datetime import timedelta
    
    # 获取各模块数据
    decorations = Decoration.objects.all()
    customer_credits = CustomerCredit.objects.all()
    customer_complaints = CustomerComplaint.objects.all()
    complaints = Complaint.objects.all()
    regulations = Regulation.objects.all()
    price_evaluations = PriceEvaluation.objects.all()
    insurances = Insurance.objects.all()
    daily_records = _my_daily_records(request.user)
    
    # 计算统计数据（同一模型的多次 count 合并为一次 aggregate，数值与原逻辑完全一致）
    total_decorations = decorations.count()
    decoration_reduced_total = sum(dec.reduced_price or 0 for dec in decorations)
    decoration_list_count = total_decorations
    
    # 客户账期统计
    credit_stats = customer_credits.aggregate(
        total=Count('id'),
        pending_site=Count('id', filter=Q(work_method='现场审核', result='待排期去现场')),
        agree=Count('id', filter=Q(result='同意')),
        disagree=Count('id', filter=Q(result='不同意')),
        distinct_customers=Count('customer_name', distinct=True),
    )
    credit_total_count = credit_stats['total']
    credit_pending_site_count = credit_stats['pending_site']
    credit_agree_count = credit_stats['agree']
    credit_disagree_count = credit_stats['disagree']
    total_customers = credit_stats['distinct_customers']
    
    # 客户客诉统计
    complaint_stats = customer_complaints.aggregate(
        total=Count('id'),
        completed=Count('id', filter=Q(complaint_status='已完成')),
        incomplete=Count('id', filter=Q(complaint_status__in=['未完成', '处理中'])),
        pending=Count('id', filter=Q(complaint_status='未完成')),
    )
    complaint_total_count = complaint_stats['total']
    complaint_completed_count = complaint_stats['completed']
    complaint_incomplete_count = complaint_stats['incomplete']
    complaint_pending_count = complaint_stats['pending']
    
    # 诉讼统计（exclude(progress='结案') 与 filter(~Q(progress='结案')) SQL 等价）
    lawsuit_stats = complaints.aggregate(
        total=Count('id'),
        completed=Count('id', filter=Q(progress='结案')),
        incomplete=Count('id', filter=~Q(progress='结案')),
    )
    lawsuit_total_count = lawsuit_stats['total']
    lawsuit_completed_count = lawsuit_stats['completed']
    lawsuit_incomplete_count = lawsuit_stats['incomplete']
    lawsuit_pending_count = lawsuit_stats['incomplete']
    
    # 核价统计
    price_total_count = price_evaluations.count()
    price_reduction_total = sum(eval.get_reduction_amount() for eval in price_evaluations)
    
    # 保险统计
    today = date.today()
    insurance_stats = insurances.aggregate(
        total=Count('id'),
        not_expired=Count('id', filter=Q(end_date__gt=today)),
    )
    insurance_total_count = insurance_stats['total']
    insurance_not_expired_count = insurance_stats['not_expired']
    
    # 每日记录统计
    daily_stats = daily_records.aggregate(
        total=Count('id'),
        incomplete=Count('id', filter=Q(status='未完成')),
        completed=Count('id', filter=Q(status='完成')),
    )
    daily_total_count = daily_stats['total']
    daily_incomplete_count = daily_stats['incomplete']
    daily_pending_count = daily_stats['incomplete']
    
    total_regulations = regulations.count()
    regulation_list_count = total_regulations
    regulation_recent_count = min(5, total_regulations)
    
    # 总记录数：口径统一在 records/stats.py（含试样/档案/知识库三个模块，
    # 改造前漏计，导致首页与看板两个"总数"——审计 #21）
    from . import stats as stats_svc
    _records_total = (
        total_decorations + total_customers + complaint_total_count + lawsuit_total_count
        + total_regulations + price_total_count + insurance_total_count + daily_total_count
    )
    _visible = set(visible_modules(request.user))
    total_records = stats_svc.total_records(_visible, _records_total, user=request.user)

    # 看板 10 个模块卡中当前用户可见的数量（原为模板硬编码 8，新增模块/按权限过滤不计）
    _DASHBOARD_MODULE_KEYS = (
        'decoration', 'customer_credit', 'customer_complaint', 'complaint',
        'regulation', 'price_evaluation', 'insurance', 'daily_record',
        'samples', 'archive',
    )
    module_count = sum(1 for k in _DASHBOARD_MODULE_KEYS if k in _visible)
    
    # 计算完成率（基于实际数据，等价于原 exists()+count() 逻辑）
    total_processed = daily_stats['completed'] + complaint_stats['completed']
    total_items = daily_stats['total'] + complaint_stats['total']
    
    # 计算完成率
    completion_rate = int((total_processed / total_items) * 100) if total_items > 0 else 0
    
    # 计算待处理数量（基于实际数据）
    pending_count = daily_pending_count + complaint_pending_count + lawsuit_pending_count
    
    # 获取待处理项目列表（用于详情展示）
    pending_items = []
    
    # 每日记录待处理项目
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
    
    # 客户客诉待处理项目
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
    
    # 诉讼待处理项目（除了结案之外的都算待处理）
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
    
    # 排序：有日期的按时间倒序在前；无日期项（诉讼）排在最后并保持 id 倒序。
    # 改造前把 '立案中' 当日期字符串与 '2026-10-18' 混排，字符串恒大于日期，
    # 导致诉讼永远占据首页第一屏（审计 #37）。
    dated = [i for i in pending_items if i['sort_at'] is not None]
    undated = [i for i in pending_items if i['sort_at'] is None]
    dated.sort(key=lambda x: x['sort_at'], reverse=True)
    pending_items = dated + undated
    
    # 近 7 天每日记录数趋势（含今天，升序，缺数的天补 0，共 7 项）
    weekly_trend = _weekly_trend(request.user)

    # ===== P5 第二步：模块徽标真实化 + 图表区数据 =====
    today_d = timezone.localdate()

    # 客诉：未闭环中已超 SLA 的数量（SLA 默认 48h，可按单覆盖）
    cc_open_qs = customer_complaints.filter(complaint_status__in=['未完成', '处理中'])
    complaint_sla_overdue_count = sum(1 for c in cc_open_qs if c.is_sla_overdue)

    # 诉讼：答辩期剩余 0~3 天（立案 + 15 天）
    lawsuit_answer_soon_count = 0
    for case in complaints.exclude(progress='结案'):
        answer_deadline = case.answer_deadline
        if answer_deadline and 0 <= (answer_deadline - today_d).days <= 3:
            lawsuit_answer_soon_count += 1

    # 保险：已过期 / 30 天内到期（分开口径，避免把过期混算成临期）
    insurance_expired_count = insurances.filter(end_date__lt=today_d).count()
    insurance_expiring_count = insurances.filter(
        end_date__gte=today_d, end_date__lte=today_d + timedelta(days=30)
    ).count()

    # 每日记录：已过截止日期仍未完成
    daily_overdue_count = daily_records.filter(
        status='未完成', due_date__lt=today_d
    ).count()

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
                f'{insurance_expired_count} 份已过期' if insurance_expired_count
                else (f'{insurance_expiring_count} 份临期' if insurance_expiring_count else '正常')
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
    if 'archive' in _visible:
        from archive.models import Archive
        module_chart_items.append(('档案', Archive.objects.count()))
    if 'samples' in _visible:
        from samples.models import Sample
        module_chart_items.append(('样品', Sample.objects.filter(is_deleted=False).count()))
    module_chart_items.append(('核价', price_total_count))
    module_chart_items.append(('保险', insurance_total_count))
    if 'archive' in _visible:
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
        'total_records': total_records,
        'module_count': module_count,
        'completion_rate': completion_rate,
        'pending_count': pending_count,
        'pending_items': pending_items,  # 返回所有待处理事项
        'daily_pending_count': daily_pending_count,
        'complaint_pending_count': complaint_pending_count,
        'lawsuit_pending_count': lawsuit_pending_count,
        'total_decorations': total_decorations,
        'decoration_reduced_total': decoration_reduced_total,
        'total_customers': total_customers,
        'credit_total_count': credit_total_count,
        'credit_pending_site_count': credit_pending_site_count,
        'credit_agree_count': credit_agree_count,
        'credit_disagree_count': credit_disagree_count,
        'complaint_total_count': complaint_total_count,
        'complaint_completed_count': complaint_completed_count,
        'complaint_incomplete_count': complaint_incomplete_count,
        'lawsuit_total_count': lawsuit_total_count,
        'lawsuit_completed_count': lawsuit_completed_count,
        'lawsuit_incomplete_count': lawsuit_incomplete_count,
        'total_regulations': total_regulations,
        'price_total_count': price_total_count,
        'price_reduction_total': price_reduction_total,
        'insurance_total_count': insurance_total_count,
        'insurance_not_expired_count': insurance_not_expired_count,
        'daily_total_count': daily_total_count,
        'daily_incomplete_count': daily_incomplete_count,
        'decoration_list_count': decoration_list_count,
        'regulation_list_count': regulation_list_count,
        'regulation_recent_count': regulation_recent_count,
        # 模板仅展示前 5 行，改传切片（无默认排序的模型用 -id，有默认排序的沿用原顺序）
        'decorations': decorations.order_by('-id')[:5],
        'customer_credits': customer_credits.order_by('-id')[:5],
        'customer_complaints': customer_complaints.order_by('-id')[:5],
        'complaints': complaints[:5],
        'regulations': regulations.order_by('-id')[:5],
        'price_evaluations': price_evaluations.order_by('-id')[:5],
        'insurances': insurances[:5],
        'daily_records': daily_records[:5],
        'weekly_trend': weekly_trend,
        'trend_max': max([x['c'] for x in weekly_trend] + [1]),
        'module_badges': module_badges,
        'chart_payload': chart_payload,
        'visible_module_keys': _visible,
        'sample_total_count': sample_total_count,
        'sample_active_count': sample_active_count,
        'sample_recent': sample_recent,
        'archive_total_count': archive_total_count,
        'archive_borrowed_count': archive_borrowed_count,
        'archive_recent': archive_recent,
    }
    
    return render(request, 'records/dashboard.html', context)


@perm_required("data_view", "read")
def data_download(request):
    """数据下载页面视图"""
    # 统计各模块数据数量
    decoration_count = Decoration.objects.count()
    customer_credit_count = CustomerCredit.objects.count()
    customer_complaint_count = CustomerComplaint.objects.count()
    complaint_count = Complaint.objects.count()
    regulation_count = Regulation.objects.count()
    price_evaluation_count = PriceEvaluation.objects.count()
    insurance_count = Insurance.objects.count()
    daily_record_count = _my_daily_records(request.user).count()
    
    context = {
        'decoration_count': decoration_count,
        'customer_credit_count': customer_credit_count,
        'customer_complaint_count': customer_complaint_count,
        'complaint_count': complaint_count,
        'regulation_count': regulation_count,
        'price_evaluation_count': price_evaluation_count,
        'insurance_count': insurance_count,
        'daily_record_count': daily_record_count,
    }
    
    return render(request, 'records/data_download.html', context)


# 客诉视图
complaint_list = make_list_view(
    perm_module='complaint', template_name='records/complaint_list.html',
    page_var='complaints', model=Complaint,
    search_fields=['dispute_type', 'defendant', 'case_number', 'court', 'lawyer', 'progress', 'detailed_progress', 'remarks'],
    filters=[
        ('dispute_type', 'dispute_type'),
        ('defendant', 'defendant__icontains'),
        ('case_number', 'case_number'),
        ('court', 'court'),
        ('lawyer', 'lawyer'),
        ('progress', 'progress'),
    ],
    filter_options={'dispute_types': 'dispute_type', 'courts': 'court', 'lawyers': 'lawyer'},
    extra_context={'progress_choices': [('立案', '立案'), ('审理中', '审理中'), ('判决', '判决'), ('执行', '执行'), ('结案', '结案')]},
)

# 诉讼留痕模块名为「诉讼」，页面标题为「客诉」（与原实现一致）
complaint_add, complaint_edit = make_form_view(
    model=Complaint, form_class=ComplaintForm, perm_module='complaint',
    log_label='诉讼', display_field='case_number',
    redirect_name='complaint_list', template_name='records/complaint_form.html',
    title_add='新增客诉', title_edit='编辑客诉',
)

complaint_delete = make_delete_view(
    model=Complaint, perm_module='complaint', label='客诉',
    display_field='case_number', redirect_name='complaint_list',
)

complaint_export = make_export_view(
    model=Complaint, perm_module='complaint', sheet_title='客诉记录', log_label='诉讼',
    filename_base='客诉记录', empty_text='当前无客诉记录，无法导出Excel',
    headers=['序号', '纠纷类型', '被告', '案号', '涉案金额（元）', '受理法院', '代理律师', '进度', '立案日期', '开庭日期', '判决日期', '执行日期', '结案日期', '详细进度', '备注'],
    row_values=lambda obj, serial: [
        serial, obj.dispute_type, obj.defendant, obj.case_number, obj.amount,
        obj.court, obj.lawyer, obj.progress,
        obj.filed_at.strftime('%Y-%m-%d') if obj.filed_at else '',
        obj.hearing_at.strftime('%Y-%m-%d') if obj.hearing_at else '',
        obj.judgment_at.strftime('%Y-%m-%d') if obj.judgment_at else '',
        obj.execution_at.strftime('%Y-%m-%d') if obj.execution_at else '',
        obj.closed_at.strftime('%Y-%m-%d') if obj.closed_at else '',
        obj.detailed_progress or '', obj.remarks or '',
    ],
)


@perm_required("decoration", "read")
def decoration_detail(request, pk):
    """装修记录详情视图"""
    decoration = get_object_or_404(Decoration, pk=pk)
    if _is_ajax(request):
        return _modal_detail(request, 'records/_decoration_detail_modal.html', {'decoration': decoration})
    return render(request, 'records/decoration_detail.html', {'decoration': decoration})


@perm_required("customer_credit", "read")
def customer_credit_detail(request, pk):
    """客户账期详情视图"""
    credit = get_object_or_404(CustomerCredit, pk=pk)
    if _is_ajax(request):
        return _modal_detail(request, 'records/_customer_credit_detail_modal.html', {'credit': credit})
    return render(request, 'records/customer_credit_detail.html', {'credit': credit})


@perm_required("complaint", "read")
def complaint_detail(request, pk):
    """诉讼详情视图（已取消 AJAX 弹窗，一律整页渲染）"""
    complaint = get_object_or_404(Complaint, pk=pk)
    return render(request, 'records/complaint_detail.html', {'complaint': complaint})


@perm_required("complaint", "update")
@require_POST
def complaint_advance(request, pk):
    """诉讼状态推进：更新 progress 并自动回填对应状态的日期字段（仅当该日期为空时）。

    状态推进遵守 OperationLog 统一留痕约束；日期字段一旦写入不被覆盖，保留首次
    进入该状态的时间。
    """
    complaint = get_object_or_404(Complaint, pk=pk)
    new_progress = request.POST.get('progress', '').strip()
    valid = {c[0] for c in Complaint.PROGRESS_CHOICES}
    if new_progress not in valid:
        return HttpResponseBadRequest('非法的诉讼状态')

    before_snap = snapshot(complaint)
    complaint.progress = new_progress
    # 自动回填当前状态对应的日期字段（仅首次进入时）
    date_field = Complaint.PROGRESS_DATE_FIELD.get(new_progress)
    if date_field and not getattr(complaint, date_field):
        setattr(complaint, date_field, timezone.localdate())
    complaint.save()
    log_operation(
        request, '诉讼', '状态变更', f'[诉讼]下的记录#{complaint.id}',
        f'诉讼状态推进: {complaint.case_number} → {new_progress}',
        before=before_snap, after=snapshot(complaint),
    )
    return redirect('complaint_detail', pk=pk)


# 操作留痕视图（固定每页 20 条；搜索不剥离空白，筛选值原样过滤）
operation_log_list = make_list_view(
    perm_module='system', template_name='records/operation_log_list.html',
    page_var='logs', model=OperationLog, per_page=20,
    search_fields=['operator', 'object_identifier', 'change_summary'],
    filters=[
        ('module_type', 'module_type', None, 'module_filter'),
        ('operation_type', 'operation_type', None, 'operation_filter'),
    ],
    extra_context={
        'module_choices': ['每日记录', '装修', '客户账期', '客户客诉', '诉讼',
                           '制度及处罚', '核价', '保险', '金属价格', '数据展示', '数据下载', '知识库',
                           '档案管理', '证照管理', '试样管理', '到期预警', '系统管理'],
        'operation_choices': ['新增', '更新', '删除', '编辑', '状态变更', '导入', '导出',
                              '下载', '备份', '恢复', '借阅', '归还', '移动', '年审'],
    },
)


@perm_required("price_evaluation", "read")
def price_evaluation_detail(request, pk):
    """核价详情视图"""
    evaluation = get_object_or_404(PriceEvaluation, pk=pk)
    if _is_ajax(request):
        return _modal_detail(request, 'records/_price_evaluation_detail_modal.html', {'evaluation': evaluation})
    return render(request, 'records/price_evaluation_detail.html', {'evaluation': evaluation})


@perm_required("insurance", "read")
def insurance_detail(request, pk):
    """保险详情视图"""
    insurance = get_object_or_404(Insurance, pk=pk)
    if _is_ajax(request):
        return _modal_detail(request, 'records/_insurance_detail_modal.html', {'insurance': insurance})
    return render(request, 'records/insurance_detail.html', {'insurance': insurance})