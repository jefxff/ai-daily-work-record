from django.shortcuts import render, get_object_or_404, redirect
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from datetime import date, datetime, timedelta
from .models import Decoration, CustomerCredit, CustomerComplaint, Regulation, PriceEvaluation, DailyRecord, MetalPrice, Insurance, Complaint, OperationLog
from .forms import DecorationForm, CustomerCreditForm, CustomerComplaintForm, RegulationForm, PriceEvaluationForm, InsuranceForm, ComplaintForm
from .audit import log_operation
from .crud import make_delete_view, make_export_view, make_form_view, make_list_view
from django.utils import timezone
from accounts.decorators import perm_required
from accounts.services import visible_modules


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
    """首页视图"""
    # 装修卡片：总装修数、核减金额求和
    decorations = Decoration.objects.all()
    total_decorations = decorations.count()
    decoration_reduced_total = sum(dec.reduced_price or 0 for dec in decorations)
    
    # 客户账期卡片：总客户数、同意数、不同意数
    customer_credits = CustomerCredit.objects.all()
    total_customers = customer_credits.values('customer_name').distinct().count()
    agree_count = customer_credits.filter(result='同意').count()
    disagree_count = customer_credits.filter(result='不同意').count()
    
    # 客户客诉卡片：总客诉数、未完成客诉数
    customer_complaints = CustomerComplaint.objects.all()
    total_complaints = customer_complaints.count()
    incomplete_complaints = customer_complaints.filter(complaint_status='未完成').count()
    
    # 制度及处罚卡片：总制度数
    total_regulations = Regulation.objects.count()
    
    # 核价卡片：总核价数、总核减金额
    price_evaluations = PriceEvaluation.objects.all()
    total_price_evaluations = price_evaluations.count()
    total_price_reduction = sum(eval.get_reduction_amount() for eval in price_evaluations)
    
# 每日记录：未完成工作（用annotate合并查询，避免重复count）；仅统计当前用户的记录
    from django.db.models import Count
    my_daily = _my_daily_records(request.user)
    daily_stats = my_daily.aggregate(
        total=Count('id'),
        incomplete=Count('id', filter=Q(status='未完成'))
    )
    total_daily_records = daily_stats['total']
    incomplete_daily_records_count = daily_stats['incomplete']
    incomplete_daily_records = my_daily.filter(status='未完成').order_by('-record_date')

    # 获取金属价格数据
    # 检查今天是否已经有数据
    today = datetime.now().date()
    today_prices = MetalPrice.objects.filter(crawl_date=today)

    if today_prices.exists():
        # 今天已有数据，直接从数据库读取
        all_prices = list(today_prices)
    else:
        # 今天没有数据，尝试从最近一次爬取读取（避免阻塞页面）
        last_prices = MetalPrice.objects.order_by('-crawl_date', '-crawl_time').first()
        if last_prices:
            all_prices = list(MetalPrice.objects.filter(crawl_date=last_prices.crawl_date))
        else:
            all_prices = []

    # 自定义排序：将黄金、1#白银、1#铜、1#锡、A00铝排在前面
    priority_metals = ['黄金', '1#白银', '1#铜', '1#锡', 'A00铝']
    
    def custom_sort(metal):
        metal_name = metal.metal_name
        if metal_name in priority_metals:
            return (0, priority_metals.index(metal_name))
        else:
            return (1, metal_name)
    
    all_prices.sort(key=custom_sort)
    
    # 保险到期台账数据 - 获取所有未到期的保险，按未到期天数从小到大排序
    from datetime import date
    today = date.today()
    
    # 获取所有未到期的保险（截止日期大于今天）
    expiring_insurances = Insurance.objects.filter(end_date__gt=today).order_by('end_date')  # 获取所有未到期的保险
    
    # 获取进度未完结的诉讼案件（排除已结案的案件）
    incomplete_litigations = Complaint.objects.exclude(progress='结案').order_by('-id')

    # 近 7 天每日记录数趋势（含今天，升序，缺数的天补 0，共 7 项）
    weekly_trend = _weekly_trend(request.user)

    context = {
        'total_decorations': total_decorations,
        'decoration_reduced_total': decoration_reduced_total,
        'total_customers': total_customers,
        'agree_count': agree_count,
        'disagree_count': disagree_count,
        'total_complaints': total_complaints,
        'incomplete_complaints': incomplete_complaints,
        'total_regulations': total_regulations,
        'total_price_evaluations': total_price_evaluations,
        'total_price_reduction': total_price_reduction,
        'incomplete_daily_records': incomplete_daily_records,
        'total_daily_records': total_daily_records,
        'incomplete_daily_records_count': incomplete_daily_records_count,
        'metal_prices': all_prices,
        'expiring_insurances': expiring_insurances,
        'incomplete_litigations': incomplete_litigations,
        'weekly_trend': weekly_trend,
        'trend_max': max([x['c'] for x in weekly_trend] + [1]),
    }
    return render(request, 'records/home.html', context)


def search(request):
    """搜索视图"""
    search_term = request.GET.get('search', '')
    
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
    
    # 按用户可见模块收敛搜索结果，防止越权查看无权限模块的数据
    visible = set(visible_modules(request.user))

    context = {
        'search_term': search_term,
        'decoration_results': decoration_results if 'decoration' in visible else Decoration.objects.none(),
        'customer_credit_results': customer_credit_results if 'customer_credit' in visible else CustomerCredit.objects.none(),
        'customer_complaint_results': customer_complaint_results if 'customer_complaint' in visible else CustomerComplaint.objects.none(),
        'regulation_results': regulation_results if 'regulation' in visible else Regulation.objects.none(),
        'price_evaluation_results': price_evaluation_results if 'price_evaluation' in visible else PriceEvaluation.objects.none(),
        'daily_record_results': daily_record_results if 'daily_record' in visible else DailyRecord.objects.none(),
        'insurance_results': insurance_results if 'insurance' in visible else Insurance.objects.none(),
        'metal_price_results': metal_price_results if 'metal_price' in visible else MetalPrice.objects.none(),
        'complaint_results': complaint_results if 'complaint' in visible else Complaint.objects.none(),
    }
    
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
    model=Decoration, perm_module='decoration', sheet_title='装修记录',
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
    model=CustomerCredit, perm_module='customer_credit', sheet_title='客户账期',
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
    model=CustomerComplaint, perm_module='customer_complaint', sheet_title='客户客诉',
    filename_base='客户客诉', empty_text='当前无客户客诉记录，无法导出Excel',
    headers=['客户名称', '责任业务员', '客诉申请日期', '终端客户', '产品型号', '我司出货量', '客户出货量', '客诉现象及客户诉求描述', '客户诉求（赔偿金额）', '是否报保险', '工程报告', '工程部分析原因报告', '欠缺材料', '客诉状态', '跟进情况', '备注'],
    row_values=lambda obj, serial: [
        obj.customer_name, obj.responsible_salesperson,
        obj.complaint_date.strftime('%Y-%m-%d') if obj.complaint_date else '',
        obj.terminal_customer or '', obj.product_model or '',
        obj.our_shipment or 0, obj.customer_shipment or 0,
        obj.complaint_description or '', obj.compensation_amount or 0,
        '是' if obj.is_insured else '否', obj.engineering_report or '',
        obj.engineering_analysis or '', obj.missing_materials or '',
        obj.complaint_status, obj.follow_up_status or '', obj.remarks or '',
    ],
)


@perm_required("customer_complaint", "read")
def customer_detail(request, pk):
    """客户详情视图"""
    # 获取客户客诉信息
    complaint = get_object_or_404(CustomerComplaint, pk=pk)

    # 可以在这里添加其他相关信息的查询，比如该客户的其他客诉记录等
    other_complaints = CustomerComplaint.objects.filter(customer_name=complaint.customer_name).exclude(id=pk)

    context = {
        'complaint': complaint,
        'other_complaints': other_complaints
    }
    if _is_ajax(request):
        return _modal_detail(request, 'records/_customer_detail_modal.html', context)
    return render(request, 'records/customer_detail.html', context)


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
    model=Regulation, perm_module='regulation', sheet_title='制度及处罚',
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
    model=PriceEvaluation, perm_module='price_evaluation', sheet_title='核价',
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
        DailyRecord.objects.create(
            user=request.user,
            department=department,
            work_content=work_content,
            due_date=due_date,
            status=status,
            progress=progress
        )
        log_operation(request, '每日记录', '新增', '[每日记录]下的新记录', f'新增每日记录: {work_content}')
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
                pass  # 通知失败不影响业务主流程
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
        record.department = department
        record.work_content = work_content
        record.status = status
        record.progress = progress
        record.due_date = due_date
        record.save()
        log_operation(request, '每日记录', '更新', f'[每日记录]下的记录#{record.id}', f'编辑每日记录: {record.work_content}')
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

daily_record_export = make_export_view(
    perm_module='daily_record', sheet_title='每日记录',
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
    model=Insurance, perm_module='insurance', sheet_title='保险记录',
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
    from datetime import date
    
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
    
    # 计算总记录数
    total_records = total_decorations + total_customers + complaint_total_count + lawsuit_total_count + total_regulations + price_total_count + insurance_total_count + daily_total_count
    
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
            'created_at': '立案中',
            'status': lawsuit.progress,
            'url': f'/complaint/detail/{lawsuit.id}/'
        })
    
    # 按时间排序（最近的在前）
    pending_items.sort(key=lambda x: x['created_at'], reverse=True)
    
    # 近 7 天每日记录数趋势（含今天，升序，缺数的天补 0，共 7 项）
    weekly_trend = _weekly_trend(request.user)

    context = {
        'total_records': total_records,
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
    model=Complaint, perm_module='complaint', sheet_title='客诉记录',
    filename_base='客诉记录', empty_text='当前无客诉记录，无法导出Excel',
    headers=['序号', '纠纷类型', '被告', '案号', '涉案金额（元）', '受理法院', '代理律师', '进度', '详细进度', '备注'],
    row_values=lambda obj, serial: [
        serial, obj.dispute_type, obj.defendant, obj.case_number, obj.amount,
        obj.court, obj.lawyer, obj.progress, obj.detailed_progress or '', obj.remarks or '',
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
    """诉讼详情视图"""
    complaint = get_object_or_404(Complaint, pk=pk)
    if _is_ajax(request):
        return _modal_detail(request, 'records/_complaint_detail_modal.html', {'complaint': complaint})
    return render(request, 'records/complaint_detail.html', {'complaint': complaint})


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
                           '制度及处罚', '核价', '保险', '金属价格', '数据展示', '数据下载', '知识库'],
        'operation_choices': ['新增', '更新', '删除', '编辑'],
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