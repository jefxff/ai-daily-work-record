from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.http import HttpResponse, JsonResponse, HttpResponseBadRequest, Http404
from django.views.decorators.http import require_POST
from django.template.loader import render_to_string
from django.db.models import Count, F, Q
from django.db.models.functions import TruncDate
from datetime import date, datetime, timedelta
import json
from .models import Decoration, CustomerCredit, CustomerComplaint, CustomerLedger, Regulation, PriceEvaluation, DailyRecord, MetalPrice, Insurance, Complaint, OperationLog
from .forms import DecorationForm, CustomerCreditForm, CustomerComplaintForm, CustomerComplaintClosureForm, CustomerLedgerForm, RegulationForm, PriceEvaluationForm, InsuranceForm, ComplaintForm
from . import audit_vocab
from .audit import log_operation, snapshot
from .crud import make_delete_view, make_export_view, make_form_view, make_list_view
from . import insurance_grouping, insurance_services
from . import customer_ledger_import as ledger_io
from django.utils import timezone
from accounts.decorators import perm_required, require_any_module
from accounts.services import has_perm, visible_modules
from attachments import services as attachment_services
from knowledge_base.models import KnowledgeDocument
from samples.models import Sample, Partner
from archive.models import Archive

import logging

logger = logging.getLogger(__name__)

# 页面视图已按职责拆分（复核报告 ARC-03），此处 re-export 保证 URL / 模板引用零改动
from .view_common import _get_my_record, _is_ajax, _modal_detail, _my_daily_records, _weekly_trend
from .views_dashboard import dashboard
from .views_home import home
from .views_search import search


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


def _inject_attachment_counts(objs, request):
    """列表页行钩子：批量注入每行的附件数量（见 attachments.services.inject_counts）。"""
    attachment_services.inject_counts(objs)


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
    row_hook=_inject_attachment_counts,
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
        'other_complaints': other_complaints,
        **attachment_services.panel_context(request, complaint),
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
    # 纯 queryset 视图（model=None）需显式声明排序白名单；默认仍按日期倒序
    sortable=['record_date', 'due_date', 'status', 'progress'],
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


@perm_required("daily_record", "read")
def daily_record_incomplete(request):
    """首页「未完成记录 · 查看全部」：页内弹窗列出当前用户全部未完成记录。

    首页面板只渲染最紧急的 5 条，其余记录由本视图一次性补齐；排序与首页一致
    （截止日期升序、空值置后）。非 AJAX 访问回退到列表页的「未完成」筛选。
    """
    today = timezone.localdate()
    records = list(
        _my_daily_records(request.user)
        .filter(status='未完成')
        .order_by(F('due_date').asc(nulls_last=True), '-record_date')
    )
    for rec in records:
        if rec.due_date:
            rec.days_left = (rec.due_date - today).days
            rec.is_overdue = rec.days_left < 0
        else:
            rec.days_left = None
            rec.is_overdue = False
    if _is_ajax(request):
        html = render_to_string(
            'records/_incomplete_daily_records_items.html',
            {'records': records, 'total': len(records)},
            request=request,
        )
        return JsonResponse({'ok': True, 'html': html})
    return redirect(f"{reverse('daily_record_list')}?status=未完成")


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
    after_delete=lambda obj, obj_id: attachment_services.delete_for_object(obj, obj_id),
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
    today = timezone.localdate()
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
    row_hook=_inject_attachment_counts,
)

insurance_add, insurance_edit = make_form_view(
    model=Insurance, form_class=InsuranceForm, perm_module='insurance',
    log_label='保险', display_field='company_name',
    redirect_name='insurance_list', template_name='records/insurance_form.html',
    title_add='新增保险', title_edit='编辑保险',
    after_save=lambda obj, created: insurance_services.attach_record_safe(obj),
)

def _delete_insurance_files(obj, obj_id):
    """保险删除后清理：通用附件（按删除前的 obj_id）+ 原属保单年度重算。"""
    attachment_services.delete_for_object(obj, obj_id)
    insurance_services.detach_record(obj)


insurance_delete = make_delete_view(
    model=Insurance, perm_module='insurance', label='保险',
    display_field='company_name', redirect_name='insurance_list',
    after_delete=_delete_insurance_files,
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
    row_hook=_inject_attachment_counts,
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
    after_delete=lambda obj, obj_id: attachment_services.delete_for_object(obj, obj_id),
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
    context = {
        'complaint': complaint,
        **attachment_services.panel_context(request, complaint),
    }
    return render(request, 'records/complaint_detail.html', context)


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


# 操作留痕视图（搜索不剥离空白；每页 10/30/50 与全站一致）
#
# 模块名/动作名的下拉与筛选**都来自 records.audit_vocab**（单一事实源）：
# 此前下拉是硬编码列表，已经落后于实际词表 —— 缺「通知中心」模块，也缺
# 重置密码/撤销会话/发布公告/任务配置等动作，导致新补的留痕在页面上筛不出来。
# 筛选走 expand_*，历史行里的「更新」「文档创建」「样品」等旧词同样命中。
operation_log_list = make_list_view(
    perm_module='system', template_name='records/operation_log_list.html',
    page_var='logs', model=OperationLog,
    search_fields=['operator', 'operator_username', 'object_identifier', 'change_summary'],
    filters=[
        ('module_type', audit_vocab.filter_by_module, None, 'module_filter'),
        ('operation_type', audit_vocab.filter_by_action, None, 'operation_filter'),
    ],
    extra_context={
        'module_choices': audit_vocab.MODULE_CHOICES,
        'operation_choices': audit_vocab.ACTION_CHOICES,
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
    subject = insurance_grouping.subject_for_record(
        insurance,
        list(Insurance.objects.all()),
        today=timezone.localdate(),
    )
    context = {
        'insurance': insurance,
        'subject': subject,
        'highlight_id': insurance.pk,
        **attachment_services.panel_context(request, insurance),
    }
    if _is_ajax(request):
        return _modal_detail(request, 'records/_insurance_detail_modal.html', context)
    return render(request, 'records/insurance_detail.html', context)


# ---------------------------------------------------------------- 客户台账

customer_ledger_list = make_list_view(
    perm_module='customer_ledger', template_name='records/customer_ledger_list.html',
    page_var='ledgers', model=CustomerLedger,
    search_fields=[
        'customer_name', 'responsible_salesperson', 'credit_code',
        'legal_representative', 'address', 'archive_no', 'remarks',
    ],
    filters=[
        ('customer_name', 'customer_name__icontains'),
        ('customer_category', 'customer_category'),
        ('responsible_salesperson', 'responsible_salesperson'),
        ('company_status', 'company_status'),
        ('attachment', lambda qs, value: (
            qs.filter(attachment__gt='') if value == 'yes'
            else qs.filter(Q(attachment='') | Q(attachment__isnull=True))
        )),
    ],
    filter_options={
        'salespersons': 'responsible_salesperson',
        'customer_categories': 'customer_category',
        'company_statuses': 'company_status',
    },
    extra_context={
        'attachment_choices': [('', '全部'), ('yes', '有附件'), ('no', '无附件')],
    },
)

customer_ledger_add, customer_ledger_edit = make_form_view(
    model=CustomerLedger, form_class=CustomerLedgerForm, perm_module='customer_ledger',
    log_label='客户台账', display_field='customer_name',
    redirect_name='customer_ledger_list', template_name='records/customer_ledger_form.html',
    title_add='新增客户台账', title_edit='编辑客户台账',
)


def _delete_ledger_attachment(obj, obj_id=None):
    """记录删除后同步清理 PDF 附件，避免磁盘孤儿文件。"""
    if obj.attachment:
        obj.attachment.delete(save=False)


def _delete_ledger_files(obj, obj_id):
    """客户台账删除后清理：汇总 PDF + 通用附件（按删除前的 obj_id）。"""
    _delete_ledger_attachment(obj)
    attachment_services.delete_for_object(obj, obj_id)


customer_ledger_delete = make_delete_view(
    model=CustomerLedger, perm_module='customer_ledger', label='客户台账',
    display_field='customer_name', redirect_name='customer_ledger_list',
    after_delete=_delete_ledger_files,
)

customer_ledger_export = make_export_view(
    model=CustomerLedger, perm_module='customer_ledger', sheet_title='客户台账', log_label='客户台账',
    filename_base='客户台账', empty_text='当前无客户台账记录，无法导出Excel',
    headers=ledger_io.CUSTOMER_LEDGER_HEADERS,
    row_values=ledger_io.ledger_export_row,
    column_width=18,
)


@perm_required("customer_ledger", "read")
def customer_ledger_detail(request, pk):
    ledger = get_object_or_404(CustomerLedger, pk=pk)
    if _is_ajax(request):
        return _modal_detail(request, 'records/_customer_ledger_detail_modal.html', {
            'ledger': ledger,
        })
    return render(request, 'records/customer_ledger_detail.html', {
        'ledger': ledger,
        'credit_changes': ledger.credit_changes.all()[:50],
    })


@perm_required("customer_ledger", "read")
def customer_ledger_attachment(request, pk):
    """附件下载入口：跳转到受保护媒体路由（/media/customer_ledgers/...）。

    真正的鉴权与防穿越由 work_record.urls.protected_media 统一完成，
    这里仅做记录级存在性校验，避免拿到不存在的附件地址。
    """
    ledger = get_object_or_404(CustomerLedger, pk=pk)
    if not ledger.attachment:
        raise Http404('该记录没有附件')
    return redirect(ledger.attachment.url)


@perm_required("customer_ledger", "create")
def customer_ledger_import(request):
    """导入第 1/2 步：上传 Excel → 预览（新增/更新/跳过/错误）。"""
    if request.method != 'POST':
        return render(request, 'records/customer_ledger_import_step1.html')

    upload = request.FILES.get('file')
    overwrite = request.POST.get('overwrite') == '1'
    if not upload:
        return render(request, 'records/customer_ledger_import_step1.html', {
            'error': '请选择要导入的 Excel 文件',
        })
    try:
        rows = ledger_io.parse_excel(upload)
        if not rows:
            raise ValueError('Excel 中没有可导入的数据行')
        preview = ledger_io.build_preview(rows, overwrite=overwrite)
    except ValueError as exc:
        return render(request, 'records/customer_ledger_import_step1.html', {
            'error': str(exc),
        })
    payload = json.dumps(rows, ensure_ascii=False)
    if len(payload.encode('utf-8')) > 2 * 1024 * 1024:
        return render(request, 'records/customer_ledger_import_step1.html', {
            'error': '文件内容过大，无法一次性预览；请拆分为多个 Excel 分批导入。',
        })
    return render(request, 'records/customer_ledger_import_step2.html', {
        'preview': preview,
        'payload': payload,
        'overwrite': overwrite,
        'filename': upload.name,
    })


@perm_required("customer_ledger", "create")
@require_POST
def customer_ledger_import_confirm(request):
    """导入第 3 步：确认写入（单事务全成全败）。"""
    try:
        rows = json.loads(request.POST.get('payload') or '[]')
        if not isinstance(rows, list) or not rows:
            raise ValueError('导入数据已失效，请重新上传文件')
        overwrite = request.POST.get('overwrite') == '1'
        preview = ledger_io.build_preview(rows, overwrite=overwrite)
        if preview['errors']:
            raise ValueError('导入数据校验未通过：' + '；'.join(preview['errors'][:10]))
        if preview['summary']['update'] and not has_perm(
            request.user, 'customer_ledger', 'update'
        ):
            raise ValueError('当前账号缺少编辑权限，无法更新已有记录')
        result = ledger_io.apply_rows(rows, overwrite=overwrite)
    except (ValueError, json.JSONDecodeError) as exc:
        return render(request, 'records/customer_ledger_import_step1.html', {
            'error': str(exc),
        })

    log_operation(
        request, '客户台账', '导入', '[客户台账]下的导入',
        f'导入客户台账: 新增{result["created"]} 更新{result["updated"]} 跳过{result["skipped"]}',
    )
    return render(request, 'records/customer_ledger_import_result.html', {'result': result})


@perm_required("customer_ledger", "read")
def customer_ledger_template(request):
    return ledger_io.template_response()
