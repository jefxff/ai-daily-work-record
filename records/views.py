from django.shortcuts import render, get_object_or_404, redirect
from django.core.paginator import Paginator
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.db.models import Count, Q
from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from datetime import datetime, timedelta
from .models import Decoration, CustomerCredit, CustomerComplaint, Regulation, PriceEvaluation, DailyRecord, MetalPrice, Insurance, Complaint
from .forms import DecorationForm, CustomerCreditForm, CustomerComplaintForm, RegulationForm, PriceEvaluationForm, InsuranceForm, ComplaintForm
from .audit import log_operation
from django.utils import timezone
from accounts.decorators import perm_required
from accounts.services import visible_modules


def _per_page(request):
    """列表页每页条数：仅允许 10/30/50，默认 10"""
    try:
        v = int(request.GET.get('per_page', 10))
    except (TypeError, ValueError):
        v = 10
    return v if v in (10, 30, 50) else 10


def _is_ajax(request):
    return request.headers.get('x-requested-with') == 'XMLHttpRequest'


def _modal_detail(request, template_name, context):
    """详情 AJAX 弹窗：返回局部模板 HTML"""
    html = render_to_string(template_name, context, request=request)
    return JsonResponse({'ok': True, 'html': html})


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
    
# 每日记录：未完成工作（用annotate合并查询，避免重复count）
    from django.db.models import Count
    daily_stats = DailyRecord.objects.aggregate(
        total=Count('id'),
        incomplete=Count('id', filter=Q(status='未完成'))
    )
    total_daily_records = daily_stats['total']
    incomplete_daily_records_count = daily_stats['incomplete']
    incomplete_daily_records = DailyRecord.objects.filter(status='未完成').order_by('-record_date')

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
    from datetime import date, timedelta
    today = date.today()
    
    # 获取所有未到期的保险（截止日期大于今天）
    expiring_insurances = Insurance.objects.filter(end_date__gt=today).order_by('end_date')  # 获取所有未到期的保险
    
    # 获取进度未完结的诉讼案件（排除已结案的案件）
    incomplete_litigations = Complaint.objects.exclude(progress='结案').order_by('-id')

    # 近 7 天每日记录数（含今天，升序，缺数的天补 0，共 7 项）
    # record_date 为 DateTimeField，使用 __date 按天过滤
    trend_today = timezone.localdate()
    weekly_trend = []
    for i in range(6, -1, -1):
        d = trend_today - timedelta(days=i)
        c = DailyRecord.objects.filter(record_date__date=d).count()
        weekly_trend.append({'date': d.strftime('%m-%d'), 'c': c})

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
    
    # 搜索每日记录
    daily_record_results = DailyRecord.objects.filter(
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
@perm_required("decoration", "read")
def decoration_list(request):
    """装修记录列表视图"""
    search_term = request.GET.get('search', '')
    constructor_filter = request.GET.get('constructor', '')
    decorations = Decoration.objects.all()
    
    # 搜索功能 - 全文搜索
    if search_term:
        decorations = decorations.filter(
            Q(project_name__icontains=search_term) |
            Q(constructor__icontains=search_term) |
            Q(project_address__icontains=search_term)
        )
    
    # 施工方筛选
    if constructor_filter:
        decorations = decorations.filter(constructor=constructor_filter)
    
    # 获取所有施工方列表（去重并排序）
    constructors = sorted(list(set([c.strip() for c in 
        Decoration.objects.values_list('constructor', flat=True) 
        if c and c.strip()])))
    
    # 分页
    paginator = Paginator(decorations, _per_page(request))
    page = request.GET.get('page', 1)
    decorations = paginator.get_page(page)
    
    return render(request, 'records/decoration_list.html', {
        'decorations': decorations, 
        'search_term': search_term,
        'constructor_filter': constructor_filter,
        'constructors': constructors
    })

@perm_required("decoration", "create")
def decoration_add(request):
    """新增装修记录视图"""
    from django.http import JsonResponse
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    if request.method == 'POST':
        form = DecorationForm(request.POST)
        if form.is_valid():
            obj = form.save()
            log_operation(request, '装修', '新增', f'[装修]下的记录#{obj.id}', f'新增装修记录: {obj.project_name}')
            if is_ajax:
                return JsonResponse({'ok': True, 'message': '新增成功'})
            return redirect('decoration_list')
        if is_ajax:
            from django.template.loader import render_to_string
            html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存记录'}, request=request)
            return JsonResponse({'ok': False, 'html': html}, status=422)
    else:
        form = DecorationForm()
    if is_ajax:
        from django.template.loader import render_to_string
        html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存记录'}, request=request)
        return JsonResponse({'ok': True, 'html': html})
    return render(request, 'records/decoration_form.html', {'form': form, 'title': '新增装修记录'})

@perm_required("decoration", "update")
def decoration_edit(request, pk):
    """编辑装修记录视图"""
    from django.http import JsonResponse
    decoration = get_object_or_404(Decoration, pk=pk)
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    if request.method == 'POST':
        form = DecorationForm(request.POST, instance=decoration)
        if form.is_valid():
            form.save()
            log_operation(request, '装修', '更新', f'[装修]下的记录#{decoration.id}', f'编辑装修记录: {decoration.project_name}')
            if is_ajax:
                return JsonResponse({'ok': True, 'message': '保存成功'})
            return redirect('decoration_list')
        if is_ajax:
            from django.template.loader import render_to_string
            html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存修改'}, request=request)
            return JsonResponse({'ok': False, 'html': html}, status=422)
    else:
        form = DecorationForm(instance=decoration)
    if is_ajax:
        from django.template.loader import render_to_string
        html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存修改'}, request=request)
        return JsonResponse({'ok': True, 'html': html})
    return render(request, 'records/decoration_form.html', {'form': form, 'title': '编辑装修记录'})

@perm_required("decoration", "read")
def decoration_export(request):
    """导出装修记录到Excel"""
    decorations = Decoration.objects.all()
    
    if not decorations.exists():
        return HttpResponse('当前无装修记录，无法导出Excel')
    
    wb = Workbook()
    ws = wb.active
    ws.title = '装修记录'
    
    # 表头（包含所有字段）
    headers = ['项目名称', '施工方', '项目地址', '开工日期', '原价', '核后价', '核减金额']
    for col, header in enumerate(headers, 1):
        ws[get_column_letter(col) + '1'] = header
    
    # 数据行
    for row, decoration in enumerate(decorations, 2):
        ws[f'A{row}'] = decoration.project_name
        ws[f'B{row}'] = decoration.constructor
        ws[f'C{row}'] = decoration.project_address or ''
        ws[f'D{row}'] = decoration.start_date.strftime('%Y-%m-%d') if decoration.start_date else ''
        ws[f'E{row}'] = decoration.original_price or 0
        ws[f'F{row}'] = decoration.final_price or 0
        ws[f'G{row}'] = decoration.reduced_price or 0
    
    # 设置列宽
    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 20
    
    # 生成文件名（数据库名-YYYYMMDD.xlsx）
    filename = f'装修记录-{datetime.now().strftime("%Y%m%d")}.xlsx'
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


# 客户账期视图
@perm_required("customer_credit", "read")
def customer_credit_list(request):
    """客户账期列表视图"""
    search_term = request.GET.get('search', '')
    customer_name_filter = request.GET.get('customer_name', '')
    customer_type_filter = request.GET.get('customer_type', '')
    salesperson_filter = request.GET.get('salesperson', '')
    specific_content_filter = request.GET.get('specific_content', '')
    work_method_filter = request.GET.get('work_method', '')
    
    credits = CustomerCredit.objects.all()
    
    # 搜索功能 - 全文搜索
    if search_term:
        credits = credits.filter(
            Q(customer_name__icontains=search_term) |
            Q(salesperson__icontains=search_term) |
            Q(work_content__icontains=search_term)
        )
    
    # 多条件筛选
    if customer_name_filter:
        credits = credits.filter(customer_name__icontains=customer_name_filter)
    
    if customer_type_filter:
        credits = credits.filter(customer_type=customer_type_filter)
    
    if salesperson_filter:
        credits = credits.filter(salesperson=salesperson_filter)
    
    if specific_content_filter:
        credits = credits.filter(specific_content=specific_content_filter)
    
    if work_method_filter:
        credits = credits.filter(work_method=work_method_filter)
    
    # 获取所有业务员列表（去重并排序）
    salespersons = sorted(list(set([s.strip() for s in 
        CustomerCredit.objects.values_list('salesperson', flat=True) 
        if s and s.strip()])))
    
    # 获取所有具体内容选项
    specific_content_choices = [('老客户重审', '老客户重审'), ('提升账期或额度', '提升账期或额度'), ('新客户账期', '新客户账期')]
    
    # 获取所有工作方式选项
    work_method_choices = [('刘总特批', '刘总特批'), ('现场审核', '现场审核'), ('内部流程', '内部流程')]
    
    # 获取所有客户类型选项
    customer_type_choices = [('普通客户', '普通客户'), ('代加工客户', '代加工客户')]
    
    # 分页
    paginator = Paginator(credits, _per_page(request))
    page = request.GET.get('page', 1)
    credits = paginator.get_page(page)
    
    return render(request, 'records/customer_credit_list.html', {
        'credits': credits, 
        'search_term': search_term,
        'customer_name_filter': customer_name_filter,
        'customer_type_filter': customer_type_filter,
        'salesperson_filter': salesperson_filter,
        'specific_content_filter': specific_content_filter,
        'work_method_filter': work_method_filter,
        'salespersons': salespersons,
        'specific_content_choices': specific_content_choices,
        'work_method_choices': work_method_choices,
        'customer_type_choices': customer_type_choices
    })

@perm_required("customer_credit", "create")
def customer_credit_add(request):
    """新增客户账期视图"""
    from django.http import JsonResponse
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    if request.method == 'POST':
        form = CustomerCreditForm(request.POST)
        if form.is_valid():
            obj = form.save()
            log_operation(request, '客户账期', '新增', f'[客户账期]下的记录#{obj.id}', f'新增客户账期: {obj.customer_name}')
            if is_ajax:
                return JsonResponse({'ok': True, 'message': '新增成功'})
            return redirect('customer_credit_list')
        if is_ajax:
            from django.template.loader import render_to_string
            html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存记录'}, request=request)
            return JsonResponse({'ok': False, 'html': html}, status=422)
    else:
        form = CustomerCreditForm()
    if is_ajax:
        from django.template.loader import render_to_string
        html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存记录'}, request=request)
        return JsonResponse({'ok': True, 'html': html})
    return render(request, 'records/customer_credit_form.html', {'form': form, 'title': '新增客户账期'})

@perm_required("customer_credit", "update")
def customer_credit_edit(request, pk):
    """编辑客户账期视图"""
    from django.http import JsonResponse
    credit = get_object_or_404(CustomerCredit, pk=pk)
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    if request.method == 'POST':
        form = CustomerCreditForm(request.POST, instance=credit)
        if form.is_valid():
            form.save()
            log_operation(request, '客户账期', '更新', f'[客户账期]下的记录#{credit.id}', f'编辑客户账期: {credit.customer_name}')
            if is_ajax:
                return JsonResponse({'ok': True, 'message': '保存成功'})
            return redirect('customer_credit_list')
        if is_ajax:
            from django.template.loader import render_to_string
            html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存修改'}, request=request)
            return JsonResponse({'ok': False, 'html': html}, status=422)
    else:
        form = CustomerCreditForm(instance=credit)
    if is_ajax:
        from django.template.loader import render_to_string
        html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存修改'}, request=request)
        return JsonResponse({'ok': True, 'html': html})
    return render(request, 'records/customer_credit_form.html', {'form': form, 'title': '编辑客户账期'})

@perm_required("customer_credit", "read")
def customer_credit_export(request):
    """导出客户账期到Excel"""
    credits = CustomerCredit.objects.all()
    
    if not credits.exists():
        return HttpResponse('当前无客户账期记录，无法导出Excel')
    
    wb = Workbook()
    ws = wb.active
    ws.title = '客户账期'
    
    # 表头（包含所有字段）
    headers = ['提出日期', '现场日期', '客户名称', '客户类型', '业务员', '工作内容', '具体内容', '前期调查表', '工作方式', '结果', '备注', '是否有业绩考核期', '考核期开始日期', '考核期结束日期', '考核内容']
    for col, header in enumerate(headers, 1):
        ws[get_column_letter(col) + '1'] = header

    # 数据行
    for row, credit in enumerate(credits, 2):
        ws[f'A{row}'] = credit.proposal_date.strftime('%Y-%m-%d') if credit.proposal_date else ''
        ws[f'B{row}'] = credit.site_date.strftime('%Y-%m-%d') if credit.site_date else ''
        ws[f'C{row}'] = credit.customer_name
        ws[f'D{row}'] = credit.customer_type
        ws[f'E{row}'] = credit.salesperson
        ws[f'F{row}'] = credit.work_content or ''
        ws[f'G{row}'] = credit.specific_content or ''
        ws[f'H{row}'] = credit.pre_survey or ''
        ws[f'I{row}'] = credit.work_method or ''
        ws[f'J{row}'] = credit.result
        ws[f'K{row}'] = credit.remarks or ''
        ws[f'L{row}'] = '是' if credit.has_assessment_period else '否'
        ws[f'M{row}'] = credit.assessment_start_date.strftime('%Y-%m-%d') if credit.assessment_start_date else ''
        ws[f'N{row}'] = credit.assessment_end_date.strftime('%Y-%m-%d') if credit.assessment_end_date else ''
        ws[f'O{row}'] = credit.assessment_content or ''
    
    # 设置列宽
    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 20
    
    # 生成文件名（数据库名-YYYYMMDD.xlsx）
    filename = f'客户账期-{datetime.now().strftime("%Y%m%d")}.xlsx'
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


# 客户客诉视图
@perm_required("customer_complaint", "read")
def customer_complaint_list(request):
    """客户客诉列表视图"""
    search_term = request.GET.get('search', '')
    customer_name_filter = request.GET.get('customer_name', '')
    responsible_salesperson_filter = request.GET.get('responsible_salesperson', '')
    product_model_filter = request.GET.get('product_model', '')
    is_insured_filter = request.GET.get('is_insured', '')
    complaint_status_filter = request.GET.get('complaint_status', '')
    
    complaints = CustomerComplaint.objects.all()
    
    # 搜索功能 - 全文搜索
    if search_term:
        complaints = complaints.filter(
            Q(customer_name__icontains=search_term) |
            Q(responsible_salesperson__icontains=search_term) |
            Q(product_model__icontains=search_term) |
            Q(complaint_description__icontains=search_term)
        )
    
    # 多条件筛选
    if customer_name_filter:
        complaints = complaints.filter(customer_name__icontains=customer_name_filter)
    
    if responsible_salesperson_filter:
        complaints = complaints.filter(responsible_salesperson=responsible_salesperson_filter)
    
    if product_model_filter:
        complaints = complaints.filter(product_model=product_model_filter)
    
    if is_insured_filter:
        is_insured_bool = is_insured_filter == 'true'
        complaints = complaints.filter(is_insured=is_insured_bool)
    
    if complaint_status_filter:
        complaints = complaints.filter(complaint_status=complaint_status_filter)
    
    # 获取所有责任业务员列表（去重并排序）
    responsible_salespersons = sorted(list(set([s.strip() for s in 
        CustomerComplaint.objects.values_list('responsible_salesperson', flat=True) 
        if s and s.strip()])))
    
    # 获取所有产品型号列表（去重并排序）
    product_models = sorted(list(set([p.strip() for p in 
        CustomerComplaint.objects.values_list('product_model', flat=True) 
        if p and p.strip()])))
    
    # 获取所有客诉状态选项
    complaint_status_choices = [('未完成', '未完成'), ('处理中', '处理中'), ('已完成', '已完成')]
    
    # 分页
    paginator = Paginator(complaints, _per_page(request))
    page = request.GET.get('page', 1)
    complaints = paginator.get_page(page)
    
    return render(request, 'records/customer_complaint_list.html', {
        'complaints': complaints, 
        'search_term': search_term,
        'customer_name_filter': customer_name_filter,
        'responsible_salesperson_filter': responsible_salesperson_filter,
        'product_model_filter': product_model_filter,
        'is_insured_filter': is_insured_filter,
        'complaint_status_filter': complaint_status_filter,
        'responsible_salespersons': responsible_salespersons,
        'product_models': product_models,
        'complaint_status_choices': complaint_status_choices
    })

@perm_required("customer_complaint", "create")
def customer_complaint_add(request):
    """新增客户客诉视图"""
    from django.http import JsonResponse
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    if request.method == 'POST':
        form = CustomerComplaintForm(request.POST)
        if form.is_valid():
            obj = form.save()
            log_operation(request, '客户客诉', '新增', f'[客户客诉]下的记录#{obj.id}', f'新增客户客诉: {obj.customer_name}')
            if is_ajax:
                return JsonResponse({'ok': True, 'message': '新增成功'})
            return redirect('customer_complaint_list')
        if is_ajax:
            from django.template.loader import render_to_string
            html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存记录'}, request=request)
            return JsonResponse({'ok': False, 'html': html}, status=422)
    else:
        form = CustomerComplaintForm()
    if is_ajax:
        from django.template.loader import render_to_string
        html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存记录'}, request=request)
        return JsonResponse({'ok': True, 'html': html})
    return render(request, 'records/customer_complaint_form.html', {'form': form, 'title': '新增客户客诉'})

@perm_required("customer_complaint", "update")
def customer_complaint_edit(request, pk):
    """编辑客户客诉视图"""
    from django.http import JsonResponse
    complaint = get_object_or_404(CustomerComplaint, pk=pk)
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    if request.method == 'POST':
        form = CustomerComplaintForm(request.POST, instance=complaint)
        if form.is_valid():
            form.save()
            log_operation(request, '客户客诉', '更新', f'[客户客诉]下的记录#{complaint.id}', f'编辑客户客诉: {complaint.customer_name}')
            if is_ajax:
                return JsonResponse({'ok': True, 'message': '保存成功'})
            return redirect('customer_complaint_list')
        if is_ajax:
            from django.template.loader import render_to_string
            html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存修改'}, request=request)
            return JsonResponse({'ok': False, 'html': html}, status=422)
    else:
        form = CustomerComplaintForm(instance=complaint)
    if is_ajax:
        from django.template.loader import render_to_string
        html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存修改'}, request=request)
        return JsonResponse({'ok': True, 'html': html})
    return render(request, 'records/customer_complaint_form.html', {'form': form, 'title': '编辑客户客诉'})

@perm_required("customer_complaint", "read")
def customer_complaint_export(request):
    """导出客户客诉到Excel"""
    complaints = CustomerComplaint.objects.all()
    
    if not complaints.exists():
        return HttpResponse('当前无客户客诉记录，无法导出Excel')
    
    wb = Workbook()
    ws = wb.active
    ws.title = '客户客诉'
    
    # 表头（包含所有字段）
    headers = ['客户名称', '责任业务员', '客诉申请日期', '终端客户', '产品型号', '我司出货量', '客户出货量', '客诉现象及客户诉求描述', '客户诉求（赔偿金额）', '是否报保险', '工程报告', '工程部分析原因报告', '欠缺材料', '客诉状态', '跟进情况', '备注']
    for col, header in enumerate(headers, 1):
        ws[get_column_letter(col) + '1'] = header
    
    # 数据行
    for row, complaint in enumerate(complaints, 2):
        ws[f'A{row}'] = complaint.customer_name
        ws[f'B{row}'] = complaint.responsible_salesperson
        ws[f'C{row}'] = complaint.complaint_date.strftime('%Y-%m-%d') if complaint.complaint_date else ''
        ws[f'D{row}'] = complaint.terminal_customer or ''
        ws[f'E{row}'] = complaint.product_model or ''
        ws[f'F{row}'] = complaint.our_shipment or 0
        ws[f'G{row}'] = complaint.customer_shipment or 0
        ws[f'H{row}'] = complaint.complaint_description or ''
        ws[f'I{row}'] = complaint.compensation_amount or 0
        ws[f'J{row}'] = '是' if complaint.is_insured else '否'
        ws[f'K{row}'] = complaint.engineering_report or ''
        ws[f'L{row}'] = complaint.engineering_analysis or ''
        ws[f'M{row}'] = complaint.missing_materials or ''
        ws[f'N{row}'] = complaint.complaint_status
        ws[f'O{row}'] = complaint.follow_up_status or ''
        ws[f'P{row}'] = complaint.remarks or ''
    
    # 设置列宽
    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 20
    
    # 生成文件名（数据库名-YYYYMMDD.xlsx）
    filename = f'客户客诉-{datetime.now().strftime("%Y%m%d")}.xlsx'
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


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
@perm_required("regulation", "read")
def regulation_list(request):
    """制度及处罚列表视图"""
    search_term = request.GET.get('search', '')
    regulations = Regulation.objects.all()
    
    # 搜索功能 - 全文搜索
    if search_term:
        regulations = regulations.filter(
            Q(title__icontains=search_term) |
            Q(content__icontains=search_term) |
            Q(doc_no__icontains=search_term)
        )
    
    # 分页
    paginator = Paginator(regulations, _per_page(request))
    page = request.GET.get('page', 1)
    regulations = paginator.get_page(page)
    
    return render(request, 'records/regulation_list.html', {'regulations': regulations, 'search_term': search_term})

@perm_required("regulation", "create")
def regulation_add(request):
    """新增制度及处罚视图"""
    from django.http import JsonResponse
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    if request.method == 'POST':
        form = RegulationForm(request.POST)
        if form.is_valid():
            obj = form.save()
            log_operation(request, '制度及处罚', '新增', f'[制度及处罚]下的记录#{obj.id}', f'新增制度及处罚: {obj.title}')
            if is_ajax:
                return JsonResponse({'ok': True, 'message': '新增成功'})
            return redirect('regulation_list')
        if is_ajax:
            from django.template.loader import render_to_string
            html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存记录'}, request=request)
            return JsonResponse({'ok': False, 'html': html}, status=422)
    else:
        form = RegulationForm()
    if is_ajax:
        from django.template.loader import render_to_string
        html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存记录'}, request=request)
        return JsonResponse({'ok': True, 'html': html})
    return render(request, 'records/regulation_form.html', {'form': form, 'title': '新增制度及处罚'})

@perm_required("regulation", "update")
def regulation_edit(request, pk):
    """编辑制度及处罚视图"""
    from django.http import JsonResponse
    regulation = get_object_or_404(Regulation, pk=pk)
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    if request.method == 'POST':
        form = RegulationForm(request.POST, instance=regulation)
        if form.is_valid():
            form.save()
            log_operation(request, '制度及处罚', '更新', f'[制度及处罚]下的记录#{regulation.id}', f'编辑制度及处罚: {regulation.title}')
            if is_ajax:
                return JsonResponse({'ok': True, 'message': '保存成功'})
            return redirect('regulation_list')
        if is_ajax:
            from django.template.loader import render_to_string
            html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存修改'}, request=request)
            return JsonResponse({'ok': False, 'html': html}, status=422)
    else:
        form = RegulationForm(instance=regulation)
    if is_ajax:
        from django.template.loader import render_to_string
        html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存修改'}, request=request)
        return JsonResponse({'ok': True, 'html': html})
    return render(request, 'records/regulation_form.html', {'form': form, 'title': '编辑制度及处罚'})

@perm_required("regulation", "read")
def regulation_export(request):
    """导出制度及处罚到Excel"""
    regulations = Regulation.objects.all()
    
    if not regulations.exists():
        return HttpResponse('当前无制度及处罚记录，无法导出Excel')
    
    wb = Workbook()
    ws = wb.active
    ws.title = '制度及处罚'
    
    # 表头
    headers = ['出文日期', '标题', '主要内容', '文件编号']
    for col, header in enumerate(headers, 1):
        ws[get_column_letter(col) + '1'] = header
    
    # 数据行
    for row, regulation in enumerate(regulations, 2):
        ws[f'A{row}'] = regulation.issue_date.strftime('%Y-%m-%d') if regulation.issue_date else ''
        ws[f'B{row}'] = regulation.title
        ws[f'C{row}'] = regulation.content or ''
        ws[f'D{row}'] = regulation.doc_no
    
    # 设置列宽
    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 30
    
    # 生成文件名（数据库名-YYYYMMDD.xlsx）
    filename = f'制度及处罚-{datetime.now().strftime("%Y%m%d")}.xlsx'
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


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
@perm_required("price_evaluation", "read")
def price_evaluation_list(request):
    """核价列表视图"""
    search_term = request.GET.get('search', '')
    evaluations = PriceEvaluation.objects.all()
    
    # 搜索功能 - 全文搜索
    if search_term:
        evaluations = evaluations.filter(
            Q(project_category__icontains=search_term) |
            Q(proposing_department__icontains=search_term) |
            Q(proposer__icontains=search_term) |
            Q(evaluation_content__icontains=search_term)
        )
    
    # 分页
    paginator = Paginator(evaluations, _per_page(request))
    page = request.GET.get('page', 1)
    evaluations = paginator.get_page(page)
    
    return render(request, 'records/price_evaluation_list.html', {'evaluations': evaluations, 'search_term': search_term})

@perm_required("price_evaluation", "create")
def price_evaluation_add(request):
    """新增核价视图"""
    from django.http import JsonResponse
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    if request.method == 'POST':
        form = PriceEvaluationForm(request.POST)
        if form.is_valid():
            obj = form.save()
            log_operation(request, '核价', '新增', f'[核价]下的记录#{obj.id}', f'新增核价: {obj.project_name}')
            if is_ajax:
                return JsonResponse({'ok': True, 'message': '新增成功'})
            return redirect('price_evaluation_list')
        if is_ajax:
            from django.template.loader import render_to_string
            html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存记录'}, request=request)
            return JsonResponse({'ok': False, 'html': html}, status=422)
    else:
        form = PriceEvaluationForm()
    if is_ajax:
        from django.template.loader import render_to_string
        html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存记录'}, request=request)
        return JsonResponse({'ok': True, 'html': html})
    return render(request, 'records/price_evaluation_form.html', {'form': form, 'title': '新增核价'})

@perm_required("price_evaluation", "update")
def price_evaluation_edit(request, pk):
    """编辑核价视图"""
    from django.http import JsonResponse
    evaluation = get_object_or_404(PriceEvaluation, pk=pk)
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    if request.method == 'POST':
        form = PriceEvaluationForm(request.POST, instance=evaluation)
        if form.is_valid():
            form.save()
            log_operation(request, '核价', '更新', f'[核价]下的记录#{evaluation.id}', f'编辑核价: {evaluation.project_name}')
            if is_ajax:
                return JsonResponse({'ok': True, 'message': '保存成功'})
            return redirect('price_evaluation_list')
        if is_ajax:
            from django.template.loader import render_to_string
            html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存修改'}, request=request)
            return JsonResponse({'ok': False, 'html': html}, status=422)
    else:
        form = PriceEvaluationForm(instance=evaluation)
    if is_ajax:
        from django.template.loader import render_to_string
        html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存修改'}, request=request)
        return JsonResponse({'ok': True, 'html': html})
    return render(request, 'records/price_evaluation_form.html', {'form': form, 'title': '编辑核价'})

@perm_required("price_evaluation", "read")
def price_evaluation_export(request):
    """导出核价到Excel"""
    evaluations = PriceEvaluation.objects.all()
    
    if not evaluations.exists():
        return HttpResponse('当前无核价记录，无法导出Excel')
    
    wb = Workbook()
    ws = wb.active
    ws.title = '核价'
    
    # 表头（包含所有字段）
    headers = ['项目分类', '项目提出部门', '提出人', '核价内容', '供应商1名称', '供应商1报价', '供应商2名称', '供应商2报价', '供应商3名称', '供应商3报价', '内控审核供应商', '内控审核价']
    for col, header in enumerate(headers, 1):
        ws[get_column_letter(col) + '1'] = header
    
    # 数据行
    for row, evaluation in enumerate(evaluations, 2):
        ws[f'A{row}'] = evaluation.project_category
        ws[f'B{row}'] = evaluation.proposing_department
        ws[f'C{row}'] = evaluation.proposer
        ws[f'D{row}'] = evaluation.evaluation_content or ''
        ws[f'E{row}'] = evaluation.supplier1_name or ''
        ws[f'F{row}'] = evaluation.supplier1_price or 0
        ws[f'G{row}'] = evaluation.supplier2_name or ''
        ws[f'H{row}'] = evaluation.supplier2_price or 0
        ws[f'I{row}'] = evaluation.supplier3_name or ''
        ws[f'J{row}'] = evaluation.supplier3_price or 0
        ws[f'K{row}'] = evaluation.internal_control_supplier or ''
        ws[f'L{row}'] = evaluation.internal_control_price
    
    # 设置列宽
    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 20
    
    # 生成文件名（数据库名-YYYYMMDD.xlsx）
    filename = f'核价-{datetime.now().strftime("%Y%m%d")}.xlsx'
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


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

        if errors:
            if is_ajax:
                from django.template.loader import render_to_string
                html = render_to_string('records/daily_record_form_modal.html', {
                    'record': None,
                    'now': timezone.now(),
                    'errors': errors,
                    'form_data': {'department': department, 'work_content': work_content, 'status': status, 'progress': progress_raw},
                }, request=request)
                return JsonResponse({'ok': False, 'html': html}, status=422)
            return render(request, 'records/daily_record_form.html', {'errors': errors})

        # 创建并保存记录
        DailyRecord.objects.create(
            department=department,
            work_content=work_content,
            status=status,
            progress=progress
        )
        log_operation(request, '每日记录', '新增', '[每日记录]下的新记录', f'新增每日记录: {work_content}')
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

@perm_required("daily_record", "read")
def daily_record_list(request):
    """每日记录列表视图"""
    search_term = request.GET.get('search', '')
    department_filter = request.GET.get('department', '')
    status_filter = request.GET.get('status', '')
    
    records = DailyRecord.objects.all().order_by('-record_date')
    
    # 搜索功能
    if search_term:
        records = records.filter(
            Q(work_content__icontains=search_term) | 
            Q(department__icontains=search_term)
        )
    
    # 部门筛选
    if department_filter:
        records = records.filter(department=department_filter)
    
    # 状态筛选
    if status_filter:
        records = records.filter(status=status_filter)
    
    # 获取所有部门列表（去重并排序）
    departments = DailyRecord.objects.values_list('department', flat=True)
    # 去除空白和空字符串，并进行规范化处理，使用set确保去重
    departments = sorted(list(set([dept.strip() for dept in departments if dept and dept.strip()])))
    
    # 分页
    paginator = Paginator(records, _per_page(request))
    page = request.GET.get('page', 1)
    records = paginator.get_page(page)
    
    return render(request, 'records/daily_record_list.html', {
        'records': records, 
        'search_term': search_term,
        'department_filter': department_filter,
        'status_filter': status_filter,
        'departments': departments,
        'status_choices': [('完成', '完成'), ('未完成', '未完成')]
    })

@perm_required("daily_record", "update")
def daily_record_edit(request, pk):
    """编辑每日记录视图"""
    from django.http import JsonResponse
    record = get_object_or_404(DailyRecord, pk=pk)
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    if request.method == 'POST':
        department = (request.POST.get('department') or '').strip()
        work_content = (request.POST.get('work_content') or '').strip()
        status = request.POST.get('status') or '未完成'
        progress_raw = request.POST.get('progress', '0') or '0'

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

        if errors:
            if is_ajax:
                # 写回用户输入的值（不持久化），重渲染局部模板
                record.department = department
                record.work_content = work_content
                record.status = status
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
    record = get_object_or_404(DailyRecord, pk=pk)
    if _is_ajax(request):
        return _modal_detail(request, 'records/_daily_record_detail_modal.html', {'record': record})
    return render(request, 'records/daily_record_detail.html', {'record': record})

@perm_required("daily_record", "read")
def daily_record_export(request):
    """导出每日记录到Excel"""
    records = DailyRecord.objects.all()
    
    if not records.exists():
        return HttpResponse('当前无每日记录，无法导出Excel')
    
    wb = Workbook()
    ws = wb.active
    ws.title = '每日记录'
    
    # 表头
    headers = ['序号', '日期及时间', '提出部门', '工作内容', '工作状态', '工作进度']
    for col, header in enumerate(headers, 1):
        ws[get_column_letter(col) + '1'] = header
    
    # 数据行
    for row, record in enumerate(records, 2):
        ws[f'A{row}'] = row - 1  # 自增序号
        ws[f'B{row}'] = record.record_date.strftime('%Y-%m-%d %H:%M:%S')
        ws[f'C{row}'] = record.department
        ws[f'D{row}'] = record.work_content
        ws[f'E{row}'] = record.status
        ws[f'F{row}'] = f'{record.progress}%'
    
    # 设置列宽
    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 20
    
    # 生成文件名（数据库名-YYYYMMDD.xlsx）
    filename = f'每日记录-{datetime.now().strftime("%Y%m%d")}.xlsx'
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


# 删除功能视图
@perm_required("decoration", "delete")
def decoration_delete(request, pk):
    """删除装修记录"""
    decoration = get_object_or_404(Decoration, pk=pk)
    log_operation(request, '装修', '删除', f'[装修]下的记录#{decoration.id}', f'删除装修记录: {decoration.project_name}')
    decoration.delete()
    return redirect('decoration_list')

@perm_required("customer_credit", "delete")
def customer_credit_delete(request, pk):
    """删除客户账期记录"""
    credit = get_object_or_404(CustomerCredit, pk=pk)
    log_operation(request, '客户账期', '删除', f'[客户账期]下的记录#{credit.id}', f'删除客户账期: {credit.customer_name}')
    credit.delete()
    return redirect('customer_credit_list')

@perm_required("customer_complaint", "delete")
def customer_complaint_delete(request, pk):
    """删除客户客诉记录"""
    complaint = get_object_or_404(CustomerComplaint, pk=pk)
    log_operation(request, '客户客诉', '删除', f'[客户客诉]下的记录#{complaint.id}', f'删除客户客诉: {complaint.customer_name}')
    complaint.delete()
    return redirect('customer_complaint_list')

@perm_required("regulation", "delete")
def regulation_delete(request, pk):
    """删除制度及处罚记录"""
    regulation = get_object_or_404(Regulation, pk=pk)
    log_operation(request, '制度及处罚', '删除', f'[制度及处罚]下的记录#{regulation.id}', f'删除制度及处罚: {regulation.title}')
    regulation.delete()
    return redirect('regulation_list')

@perm_required("price_evaluation", "delete")
def price_evaluation_delete(request, pk):
    """删除核价记录"""
    evaluation = get_object_or_404(PriceEvaluation, pk=pk)
    log_operation(request, '核价', '删除', f'[核价]下的记录#{evaluation.id}', f'删除核价: {evaluation.project_name}')
    evaluation.delete()
    return redirect('price_evaluation_list')

@perm_required("daily_record", "delete")
def daily_record_delete(request, pk):
    """删除每日记录"""
    record = get_object_or_404(DailyRecord, pk=pk)
    log_operation(request, '每日记录', '删除', f'[每日记录]下的记录#{record.id}', f'删除每日记录: {record.work_content}')
    record.delete()
    return redirect('daily_record_list')


# 保险视图
@perm_required("insurance", "read")
def insurance_list(request):
    """保险列表视图"""
    search_term = request.GET.get('search', '')
    company_name_filter = request.GET.get('company_name', '')
    title_filter = request.GET.get('title', '')
    department_filter = request.GET.get('department', '')
    is_expired_filter = request.GET.get('is_expired', '')
    date_start = request.GET.get('date_start', '')
    date_end = request.GET.get('date_end', '')
    
    insurances = Insurance.objects.all()
    
    # 搜索功能 - 全文搜索
    if search_term:
        insurances = insurances.filter(
            Q(company_name__icontains=search_term) |
            Q(title__icontains=search_term) |
            Q(order_number__icontains=search_term) |
            Q(department__icontains=search_term) |
            Q(remarks__icontains=search_term)
        )
    
    # 多条件筛选
    if company_name_filter:
        insurances = insurances.filter(company_name__icontains=company_name_filter)
    
    if title_filter:
        insurances = insurances.filter(title__icontains=title_filter)
    
    if department_filter:
        insurances = insurances.filter(department=department_filter)
    
    if is_expired_filter:
        from datetime import date
        today = date.today()
        if is_expired_filter == 'expired':
            insurances = insurances.filter(end_date__lt=today)
        elif is_expired_filter == 'not_expired':
            insurances = insurances.filter(end_date__gte=today)
    
    # 日期范围筛选
    if date_start:
        insurances = insurances.filter(end_date__gte=date_start)
    if date_end:
        insurances = insurances.filter(end_date__lte=date_end)
    
    # 获取所有部门列表（去重并排序）
    departments = sorted(list(set([d.strip() for d in 
        Insurance.objects.values_list('department', flat=True) 
        if d and d.strip()])))
    
    # 分页
    paginator = Paginator(insurances, _per_page(request))
    page = request.GET.get('page', 1)
    insurances = paginator.get_page(page)
    
    return render(request, 'records/insurance_list.html', {
        'insurances': insurances, 
        'search_term': search_term,
        'company_name_filter': company_name_filter,
        'title_filter': title_filter,
        'department_filter': department_filter,
        'is_expired_filter': is_expired_filter,
        'date_start': date_start,
        'date_end': date_end,
        'departments': departments
    })

@perm_required("insurance", "create")
def insurance_add(request):
    """新增保险视图"""
    from django.http import JsonResponse
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    if request.method == 'POST':
        form = InsuranceForm(request.POST)
        if form.is_valid():
            obj = form.save()
            log_operation(request, '保险', '新增', f'[保险]下的记录#{obj.id}', f'新增保险: {obj.company_name}')
            if is_ajax:
                return JsonResponse({'ok': True, 'message': '新增成功'})
            return redirect('insurance_list')
        if is_ajax:
            from django.template.loader import render_to_string
            html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存记录'}, request=request)
            return JsonResponse({'ok': False, 'html': html}, status=422)
    else:
        form = InsuranceForm()
    if is_ajax:
        from django.template.loader import render_to_string
        html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存记录'}, request=request)
        return JsonResponse({'ok': True, 'html': html})
    return render(request, 'records/insurance_form.html', {'form': form, 'title': '新增保险'})

@perm_required("insurance", "update")
def insurance_edit(request, pk):
    """编辑保险视图"""
    from django.http import JsonResponse
    insurance = get_object_or_404(Insurance, pk=pk)
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    if request.method == 'POST':
        form = InsuranceForm(request.POST, instance=insurance)
        if form.is_valid():
            form.save()
            log_operation(request, '保险', '更新', f'[保险]下的记录#{insurance.id}', f'编辑保险: {insurance.company_name}')
            if is_ajax:
                return JsonResponse({'ok': True, 'message': '保存成功'})
            return redirect('insurance_list')
        if is_ajax:
            from django.template.loader import render_to_string
            html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存修改'}, request=request)
            return JsonResponse({'ok': False, 'html': html}, status=422)
    else:
        form = InsuranceForm(instance=insurance)
    if is_ajax:
        from django.template.loader import render_to_string
        html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存修改'}, request=request)
        return JsonResponse({'ok': True, 'html': html})
    return render(request, 'records/insurance_form.html', {'form': form, 'title': '编辑保险'})

@perm_required("insurance", "delete")
def insurance_delete(request, pk):
    """删除保险视图"""
    insurance = get_object_or_404(Insurance, pk=pk)
    log_operation(request, '保险', '删除', f'[保险]下的记录#{insurance.id}', f'删除保险: {insurance.company_name}')
    insurance.delete()
    return redirect('insurance_list')

@perm_required("insurance", "read")
def insurance_export(request):
    """导出保险到Excel"""
    insurances = Insurance.objects.all()
    
    if not insurances.exists():
        return HttpResponse('当前无保险记录，无法导出Excel')
    
    wb = Workbook()
    ws = wb.active
    ws.title = '保险记录'
    
    # 表头
    headers = ['序号', '公司名称', '标题', '单号', '保费', '签发日期', '截止日期', '是否已经到期', '部门', '备注']
    for col, header in enumerate(headers, 1):
        ws[get_column_letter(col) + '1'] = header
    
    # 数据行
    for row, insurance in enumerate(insurances, 2):
        ws[f'A{row}'] = row - 1  # 自增序号
        ws[f'B{row}'] = insurance.company_name
        ws[f'C{row}'] = insurance.title
        ws[f'D{row}'] = insurance.order_number or ''
        ws[f'E{row}'] = insurance.premium
        ws[f'F{row}'] = insurance.issue_date.strftime('%Y-%m-%d')
        ws[f'G{row}'] = insurance.end_date.strftime('%Y-%m-%d')
        ws[f'H{row}'] = insurance.expired_days
        ws[f'I{row}'] = insurance.department
        ws[f'J{row}'] = insurance.remarks or ''
    
    # 设置列宽
    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 20
    
    # 生成文件名（数据库名-YYYYMMDD.xlsx）
    filename = f'保险记录-{datetime.now().strftime("%Y%m%d")}.xlsx'
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


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


def test_view(request):
    """测试视图"""
    return HttpResponse('Hello, World! This is a test view.')


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
    daily_records = DailyRecord.objects.all()
    
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
    
    # 近 7 天每日记录数（含今天，升序，缺数的天补 0，共 7 项）
    # record_date 为 DateTimeField，使用 __date 按天过滤
    trend_today = timezone.localdate()
    weekly_trend = []
    for i in range(6, -1, -1):
        d = trend_today - timedelta(days=i)
        c = DailyRecord.objects.filter(record_date__date=d).count()
        weekly_trend.append({'date': d.strftime('%m-%d'), 'c': c})

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
    daily_record_count = DailyRecord.objects.count()
    
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
@perm_required("complaint", "read")
def complaint_list(request):
    """客诉列表视图"""
    search_term = request.GET.get('search', '')
    dispute_type_filter = request.GET.get('dispute_type', '')
    defendant_filter = request.GET.get('defendant', '')
    case_number_filter = request.GET.get('case_number', '')
    court_filter = request.GET.get('court', '')
    lawyer_filter = request.GET.get('lawyer', '')
    progress_filter = request.GET.get('progress', '')
    
    complaints = Complaint.objects.all()
    
    # 搜索功能 - 全文搜索
    if search_term:
        complaints = complaints.filter(
            Q(dispute_type__icontains=search_term) |
            Q(defendant__icontains=search_term) |
            Q(case_number__icontains=search_term) |
            Q(court__icontains=search_term) |
            Q(lawyer__icontains=search_term) |
            Q(progress__icontains=search_term) |
            Q(detailed_progress__icontains=search_term) |
            Q(remarks__icontains=search_term)
        )
    
    # 多条件筛选
    if dispute_type_filter:
        complaints = complaints.filter(dispute_type=dispute_type_filter)
    
    if defendant_filter:
        complaints = complaints.filter(defendant__icontains=defendant_filter)
    
    if case_number_filter:
        complaints = complaints.filter(case_number=case_number_filter)
    
    if court_filter:
        complaints = complaints.filter(court=court_filter)
    
    if lawyer_filter:
        complaints = complaints.filter(lawyer=lawyer_filter)
    
    if progress_filter:
        complaints = complaints.filter(progress=progress_filter)
    
    # 获取所有纠纷类型列表（去重并排序）
    dispute_types = sorted(list(set([d.strip() for d in 
        Complaint.objects.values_list('dispute_type', flat=True) 
        if d and d.strip()])))
    
    # 获取所有受理法院列表（去重并排序）
    courts = sorted(list(set([c.strip() for c in 
        Complaint.objects.values_list('court', flat=True) 
        if c and c.strip()])))
    
    # 获取所有代理律师列表（去重并排序）
    lawyers = sorted(list(set([l.strip() for l in 
        Complaint.objects.values_list('lawyer', flat=True) 
        if l and l.strip()])))
    
    # 获取所有进度选项
    progress_choices = [('立案', '立案'), ('审理中', '审理中'), ('判决', '判决'), ('执行', '执行'), ('结案', '结案')]
    
    # 分页
    paginator = Paginator(complaints, _per_page(request))
    page = request.GET.get('page', 1)
    complaints = paginator.get_page(page)
    
    return render(request, 'records/complaint_list.html', {
        'complaints': complaints, 
        'search_term': search_term,
        'dispute_type_filter': dispute_type_filter,
        'defendant_filter': defendant_filter,
        'case_number_filter': case_number_filter,
        'court_filter': court_filter,
        'lawyer_filter': lawyer_filter,
        'progress_filter': progress_filter,
        'dispute_types': dispute_types,
        'courts': courts,
        'lawyers': lawyers,
        'progress_choices': progress_choices
    })

@perm_required("complaint", "create")
def complaint_add(request):
    """新增客诉视图"""
    from django.http import JsonResponse
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    if request.method == 'POST':
        form = ComplaintForm(request.POST)
        if form.is_valid():
            obj = form.save()
            log_operation(request, '诉讼', '新增', f'[诉讼]下的记录#{obj.id}', f'新增诉讼: {obj.case_number}')
            if is_ajax:
                return JsonResponse({'ok': True, 'message': '新增成功'})
            return redirect('complaint_list')
        if is_ajax:
            from django.template.loader import render_to_string
            html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存记录'}, request=request)
            return JsonResponse({'ok': False, 'html': html}, status=422)
    else:
        form = ComplaintForm()
    if is_ajax:
        from django.template.loader import render_to_string
        html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存记录'}, request=request)
        return JsonResponse({'ok': True, 'html': html})
    return render(request, 'records/complaint_form.html', {'form': form, 'title': '新增客诉'})

@perm_required("complaint", "update")
def complaint_edit(request, pk):
    """编辑客诉视图"""
    from django.http import JsonResponse
    complaint = get_object_or_404(Complaint, pk=pk)
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'
    if request.method == 'POST':
        form = ComplaintForm(request.POST, instance=complaint)
        if form.is_valid():
            form.save()
            log_operation(request, '诉讼', '更新', f'[诉讼]下的记录#{complaint.id}', f'编辑诉讼: {complaint.case_number}')
            if is_ajax:
                return JsonResponse({'ok': True, 'message': '保存成功'})
            return redirect('complaint_list')
        if is_ajax:
            from django.template.loader import render_to_string
            html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存修改'}, request=request)
            return JsonResponse({'ok': False, 'html': html}, status=422)
    else:
        form = ComplaintForm(instance=complaint)
    if is_ajax:
        from django.template.loader import render_to_string
        html = render_to_string('records/_form_modal.html', {'form': form, 'save_label': '保存修改'}, request=request)
        return JsonResponse({'ok': True, 'html': html})
    return render(request, 'records/complaint_form.html', {'form': form, 'title': '编辑客诉'})

@perm_required("complaint", "delete")
def complaint_delete(request, pk):
    """删除客诉视图"""
    complaint = get_object_or_404(Complaint, pk=pk)
    log_operation(request, '诉讼', '删除', f'[诉讼]下的记录#{complaint.id}', f'删除诉讼: {complaint.case_number}')
    complaint.delete()
    return redirect('complaint_list')

@perm_required("complaint", "read")
def complaint_export(request):
    """导出客诉到Excel"""
    complaints = Complaint.objects.all()
    
    if not complaints.exists():
        return HttpResponse('当前无客诉记录，无法导出Excel')
    
    wb = Workbook()
    ws = wb.active
    ws.title = '客诉记录'
    
    # 表头
    headers = ['序号', '纠纷类型', '被告', '案号', '涉案金额（元）', '受理法院', '代理律师', '进度', '详细进度', '备注']
    for col, header in enumerate(headers, 1):
        ws[get_column_letter(col) + '1'] = header
    
    # 数据行
    for row, complaint in enumerate(complaints, 2):
        ws[f'A{row}'] = row - 1  # 自增序号
        ws[f'B{row}'] = complaint.dispute_type
        ws[f'C{row}'] = complaint.defendant
        ws[f'D{row}'] = complaint.case_number
        ws[f'E{row}'] = complaint.amount
        ws[f'F{row}'] = complaint.court
        ws[f'G{row}'] = complaint.lawyer
        ws[f'H{row}'] = complaint.progress
        ws[f'I{row}'] = complaint.detailed_progress or ''
        ws[f'J{row}'] = complaint.remarks or ''
    
    # 设置列宽
    for col in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col)].width = 20
    
    # 生成文件名（数据库名-YYYYMMDD.xlsx）
    filename = f'客诉记录-{datetime.now().strftime("%Y%m%d")}.xlsx'
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


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


@perm_required("system", "read")
def operation_log_list(request):
    """操作留痕列表视图"""
    from .models import OperationLog
    search_term = request.GET.get('search', '')
    module_filter = request.GET.get('module_type', '')
    operation_filter = request.GET.get('operation_type', '')

    logs = OperationLog.objects.all()

    if search_term:
        logs = logs.filter(
            Q(operator__icontains=search_term) |
            Q(object_identifier__icontains=search_term) |
            Q(change_summary__icontains=search_term)
        )

    if module_filter:
        logs = logs.filter(module_type=module_filter)

    if operation_filter:
        logs = logs.filter(operation_type=operation_filter)

    module_choices = ['每日记录', '装修', '客户账期', '客户客诉', '诉讼', '制度及处罚', '核价', '保险', '金属价格', '数据展示', '数据下载', '知识库']
    operation_choices = ['新增', '更新', '删除', '编辑']

    paginator = Paginator(logs, 20)
    page = request.GET.get('page', 1)
    logs = paginator.get_page(page)

    return render(request, 'records/operation_log_list.html', {
        'logs': logs,
        'search_term': search_term,
        'module_filter': module_filter,
        'operation_filter': operation_filter,
        'module_choices': module_choices,
        'operation_choices': operation_choices,
    })


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