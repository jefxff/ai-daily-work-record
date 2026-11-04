"""客户评审与付款穿行测试视图。

拆成独立文件而不是继续堆在 views.py：这两块是新增的业务域（客户评审、
内控穿行测试），与原有 CRUD 无明显耦合，独立文件便于按需启停与单独测试。
"""

import json
from decimal import Decimal, ROUND_HALF_UP

from django.contrib import messages
from django.db.models import Q
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.decorators import perm_required
from attachments.services import delete_for_object, inject_counts, panel_context
from django.db import transaction
from work_record.export_guard import (
    export_too_large_response, guarded_export, max_export_rows,
)

from . import credit_spec as spec
from . import customer_review_io as review_io
from .audit import log_operation, snapshot
from .crud import make_delete_view, make_list_view
from .forms import CustomerReviewForm, PaymentWalkthroughForm
from .models import CustomerCredit, CustomerLedger, CustomerReview, PaymentWalkthrough
from .serials import next_serial

MODULE = 'customer_review'


# ---------------------------------------------------------------------------
# 公共小工具
# ---------------------------------------------------------------------------
def _dec(value, places='0.01'):
    """float/str → 两位小数 Decimal；None 原样返回。"""
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP)


def _ratio_payload(base):
    """算比率并转成可直接赋给模型字段的 Decimal 字典。"""
    raw = spec.compute_ratios(base)
    return {key: _dec(value) for key, value in raw.items()}


def _groups_for(review_type):
    if review_type == CustomerReview.TYPE_CREDIT:
        return spec.CREDIT_GROUPS, spec.CREDIT_GRADES
    if review_type == CustomerReview.TYPE_REVIEW:
        return spec.REVIEW_GROUPS, spec.REVIEW_GRADES
    return None, None


def _collect_scores(request, groups):
    """从 POST 收集评分：字段名 score_<key>，空值表示未评分。"""
    raw = {}
    prefix = 'score_'
    for key, value in request.POST.items():
        if key.startswith(prefix) and str(value).strip() != '':
            raw[key[len(prefix):]] = value
    return spec.clamp_scores(spec.parse_scores(raw), groups)


def _collect_verdicts(request):
    """现场审核判定：字段名 av_<row>，值为四档之一。"""
    allowed = set(spec.AUDIT_VERDICTS)
    out = {}
    for key, value in request.POST.items():
        if not key.startswith('av_'):
            continue
        value = str(value).strip()
        if value in allowed:
            out[key[3:]] = value
    return out


def _collect_notes(request):
    """现场审核的情况说明：字段名 an_<row>。"""
    out = {}
    for key, value in request.POST.items():
        if key.startswith('an_'):
            text = str(value).strip()
            if text:
                out[key[3:]] = text[:500]
    return out


def _base_data(form):
    """从已校验的表单取财务基础数据（万元）。"""
    keys = [f['key'] for f in spec.FIN_FIELDS]
    return {key: form.cleaned_data.get(key) for key in keys}


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------
def _review_rows(object_list, request):
    """列表页批量注入附件数（一次 GROUP BY，避免逐行 COUNT）。"""
    inject_counts(object_list)


customer_review_list = make_list_view(
    perm_module=MODULE, template_name='records/customer_review_list.html',
    page_var='reviews', model=CustomerReview,
    search_fields=['customer_name', 'customer_code', 'review_no'],
    filters=[
        ('review_type', 'review_type'),
        ('customer_type', 'customer_type'),
        ('customer_name', 'customer_name__icontains'),
        ('grade', 'grade'),
    ],
    row_hook=_review_rows,
    extra_context={
        'review_types': CustomerReview.REVIEW_TYPES,
        'customer_types': CustomerReview.CUSTOMER_TYPES,
        'credit_grades': spec.CREDIT_GRADES,
        'review_grades': spec.REVIEW_GRADES,
    },
)


def _walkthrough_rows(object_list, request):
    """列表页注入样本数与缺陷数：一次遍历 samples（JSON），不额外查库。"""
    for obj in object_list:
        samples = obj.samples if isinstance(obj.samples, list) else []
        stats = spec.walkthrough_stats(samples, obj.terms_days)
        obj.stat_samples = stats['sample_count']
        obj.stat_defects = stats['total_defects']
        obj.stat_effective = stats['effective_steps']
        obj.stat_overdue = stats['terms']['overdue']
    inject_counts(object_list)


walkthrough_list = make_list_view(
    perm_module=MODULE, template_name='records/walkthrough_list.html',
    page_var='walkthroughs', model=PaymentWalkthrough,
    search_fields=['payee_name', 'test_no'],
    filters=[
        ('payee_type', 'payee_type'),
        ('conclusion', 'conclusion'),
    ],
    row_hook=_walkthrough_rows,
    extra_context={
        'payee_types': PaymentWalkthrough.PAYEE_TYPES,
        'conclusions': spec.WALKTHROUGH_CONCLUSIONS,
    },
)


# ---------------------------------------------------------------------------
# 客户评审：新增 / 编辑
# ---------------------------------------------------------------------------
def _review_form_context(review_type, instance, form, scores=None, verdicts=None):
    groups, grades = _groups_for(review_type)
    ratios = {}
    auto_scores = {}
    if review_type == CustomerReview.TYPE_CREDIT:
        # 已保存的实例：直接用落库的比率，保证与出具时的结论一致；
        # 新建时为空，由前端随录入实时计算。
        if instance and instance.pk:
            ratios = {r['key']: getattr(instance, r['key'], None) for r in spec.RATIOS}
        auto_scores = spec.suggest_fin_scores({
            k: (float(v) if v is not None else None) for k, v in ratios.items()
        })

    listed = spec.score_items(groups, scores or {}) if groups else []

    # 财务字段按「资产负债 / 损益 / 现金流」分组，并把 BoundField 一起带过去，
    # 模板里直接 {{ fld.bound }}，避免用自定义过滤器按变量名取表单字段。
    fin_groups = []
    for group_name in spec.FIN_GROUP_ORDER:
        fields = [f for f in spec.FIN_FIELDS if f['group'] == group_name]
        fin_groups.append({
            'name': group_name,
            'fields': [{'meta': f, 'bound': form[f['key']]} for f in fields],
        })

    notes = instance.audit_notes if instance and isinstance(instance.audit_notes, dict) else {}
    audit_rows = []
    if review_type == CustomerReview.TYPE_AUDIT:
        for index, row in enumerate(spec.AUDIT_ROWS):
            key = f'r{index}'
            audit_rows.append({
                **row,
                'key': key,
                'verdict': (verdicts or {}).get(key, ''),
                'note': notes.get(key, ''),
            })

    return {
        'form': form,
        'instance': instance,
        'review_type': review_type,
        'type_title': dict(CustomerReview.REVIEW_TYPES)[review_type],
        'groups': listed,
        'grades': grades or [],
        'fin_groups': fin_groups,
        'ratios': spec.RATIOS,
        'ratio_values': ratios,
        'auto_scores': auto_scores,
        'audit_rows': audit_rows,
        'audit_verdicts_choices': spec.AUDIT_VERDICTS,
        'audit_conclusions': spec.AUDIT_CONCLUSIONS,
        'total_possible': sum(g['max'] for g in (groups or [])),
        'is_edit': bool(instance and instance.pk),
    }


def _enforce_source_snapshot(obj, request, original=None):
    """来源账期单存在时，服务端强制回写快照字段（实现细节约束 1）。

    前端 disabled 字段不随 POST 提交，只读状态不可信：
    - customer_name 仅在 POST 带 manual_name=1（勾选「手动修改」）时放开；
    - customer_type：账期单已细分 → 以账期单为准；未细分（历史单）→ 保留原值；
    - apply_no 一律以账期单当前 credit_no 为准（账期单补录单号后同步过来）。

    original：绑定表单前拍下的原值（ModelForm.is_valid() 的 _post_clean 会就地
    改写实例），账期单未细分时用它恢复，避免表单 POST 缺字段把值冲成空串。
    """
    source = obj.source_credit
    if source is None:
        return
    original = original or {}
    changed = []
    if request.POST.get('manual_name') != '1' and obj.customer_name != source.customer_name:
        obj.customer_name = source.customer_name
        changed.append('customer_name')
    expected_type = source.customer_segment or original.get('customer_type')
    if expected_type and obj.customer_type != expected_type:
        obj.customer_type = expected_type
        changed.append('customer_type')
    apply_no = source.credit_no or ''
    if obj.apply_no != apply_no:
        obj.apply_no = apply_no
        changed.append('apply_no')
    if changed:
        obj.save(update_fields=changed)


@perm_required(MODULE, 'create')
def credit_launch_review(request, pk):
    """从客户账期单发起评审（方案定稿第七节 B）。

    GET  渲染确认页：按工作方式给出默认勾选，列出每张单的预填值。
    POST 事务内一次生成勾选的评审单，全部写 source_credit 与快照，逐单留痕。
    权限挂 customer_review.create：账期读权限不等于评审创建权（实现细节约束 3）。
    """
    credit = get_object_or_404(CustomerCredit, pk=pk)
    ledger = CustomerLedger.objects.filter(customer_name=credit.customer_name).first()
    review_date = credit.site_date or credit.proposal_date
    existing = {r.review_type: r for r in credit.reviews.all()}

    if request.method == 'POST':
        chosen = [t for t in request.POST.getlist('review_types') if t in (CustomerReview.TYPE_AUDIT, CustomerReview.TYPE_CREDIT)]
        if not chosen:
            messages.error(request, '请至少勾选一张要生成的评审表')
            return redirect('customer_credit_launch_review', pk=pk)
        created = []
        with transaction.atomic():
            for review_type in chosen:
                obj = CustomerReview(
                    review_type=review_type,
                    review_no=next_serial(CustomerReview, 'review_no', 'CR'),
                    customer_name=credit.customer_name,
                    customer_type=credit.customer_segment or '',
                    review_date=review_date,
                    apply_no=credit.credit_no or '',
                    source_credit=credit,
                    created_by=request.user if request.user.is_authenticated else None,
                )
                if review_type == CustomerReview.TYPE_AUDIT:
                    obj.audit_address = (ledger.address if ledger else '') or ''
                    obj.companion = credit.salesperson or ''
                else:
                    obj.settle_method = (ledger.payment_terms if ledger else '') or ''
                obj.save()
                created.append(obj)
                log_operation(
                    request, '客户评审', '新增',
                    f'[客户评审]下的记录#{obj.id}',
                    f'发起评审生成{obj.get_review_type_display()}：{obj.customer_name}'
                    f'（来源账期 {credit.credit_no or f"#{credit.id}"}）',
                    after=snapshot(obj),
                )
        messages.success(
            request, '已生成：' + '、'.join(obj.get_review_type_display() for obj in created)
        )
        return redirect('customer_review_detail', pk=created[0].id)

    plan = []
    for review_type, title in (
        (CustomerReview.TYPE_AUDIT, '客户现场审核表'),
        (CustomerReview.TYPE_CREDIT, '客户信用评价表'),
    ):
        is_audit = review_type == CustomerReview.TYPE_AUDIT
        plan.append({
            'type': review_type,
            'title': title,
            # 默认勾选跟随工作方式：现场审核 → 两张全勾；内部流程 → 只勾信用评价；
            # 刘总特批 → 全不勾，由人工判断（特批通常无评审依据）
            'default': (credit.work_method == '现场审核') if is_audit else (
                credit.work_method in ('现场审核', '内部流程')),
            'existing': existing.get(review_type),
            'fields': [
                ('客户全称', credit.customer_name),
                ('客户类型', credit.customer_segment or '（未细分，由填表人自选）'),
                ('表单日期', review_date.strftime('%Y-%m-%d') if review_date else '—'),
                ('对应申请编号', credit.credit_no or f'账期 #{credit.id}（历史单无单号）'),
            ] + ([
                ('审核地址', (ledger.address if ledger and ledger.address else '') or '（台账无同名客户）'),
                ('陪同人员', credit.salesperson or '—'),
            ] if is_audit else [
                ('结算方式', (ledger.payment_terms if ledger and ledger.payment_terms else '') or '（台账无同名客户）'),
            ]),
        })
    context = {
        'credit': credit,
        'plan': plan,
        'has_ledger': bool(ledger),
    }
    return render(request, 'records/customer_credit_launch_review.html', context)


@perm_required(MODULE, 'create')
def review_add(request, review_type):
    if review_type not in dict(CustomerReview.REVIEW_TYPES):
        raise Http404('未知的评审表单类型')

    instance = CustomerReview(review_type=review_type, review_date=timezone.localdate())
    if request.method == 'POST':
        form = CustomerReviewForm(request.POST, instance=instance)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.review_type = review_type
            obj.created_by = request.user if request.user.is_authenticated else None

            groups, grades = _groups_for(review_type)
            if groups:
                scores = _collect_scores(request, groups)
                obj.item_scores = scores
                obj.total_score = spec.total_score(scores, groups)
                obj.grade = spec.grade_of(float(obj.total_score), grades)['label']
                if review_type == CustomerReview.TYPE_CREDIT:
                    for key, value in _ratio_payload(_base_data(form)).items():
                        setattr(obj, key, value)
            else:
                verdicts = _collect_verdicts(request)
                obj.audit_verdicts = verdicts
                obj.audit_notes = _collect_notes(request)
                conclusion, _reason = spec.audit_conclusion(verdicts)
                obj.audit_conclusion = conclusion
                obj.total_score = 0

            obj.save()
            if not obj.review_no:
                # 表单编号留空 → 自动生成 CR-YYYYMM-序号（方案定稿第六节）；
                # 手填过的不覆盖，Excel 导入按表单编号匹配因此不受影响。
                obj.review_no = next_serial(CustomerReview, 'review_no', 'CR')
                obj.save(update_fields=['review_no'])
            log_operation(
                request, '客户评审', '新增',
                f'[客户评审]下的记录#{obj.id}',
                f'新增{dict(CustomerReview.REVIEW_TYPES)[review_type]}：{obj.customer_name}',
                after=snapshot(obj),
            )
            messages.success(request, f'{obj.get_review_type_display()}已保存')
            return redirect('customer_review_detail', pk=obj.id)
    else:
        form = CustomerReviewForm(instance=instance)

    return render(request, 'records/customer_review_form.html',
                  _review_form_context(review_type, instance, form))


@perm_required(MODULE, 'update')
def review_edit(request, pk):
    obj = get_object_or_404(CustomerReview, pk=pk)
    review_type = obj.review_type

    if request.method == 'POST':
        # original 须在绑定表单前拍：is_valid() 的 _post_clean 会就地改写实例
        original = {'customer_type': obj.customer_type}
        form = CustomerReviewForm(request.POST, instance=obj)
        if form.is_valid():
            before = snapshot(obj)
            obj = form.save(commit=False)
            groups, grades = _groups_for(review_type)
            if groups:
                scores = _collect_scores(request, groups)
                obj.item_scores = scores
                obj.total_score = spec.total_score(scores, groups)
                obj.grade = spec.grade_of(float(obj.total_score), grades)['label']
                if review_type == CustomerReview.TYPE_CREDIT:
                    for key, value in _ratio_payload(_base_data(form)).items():
                        setattr(obj, key, value)
            else:
                verdicts = _collect_verdicts(request)
                obj.audit_verdicts = verdicts
                obj.audit_notes = _collect_notes(request)
                conclusion, _reason = spec.audit_conclusion(verdicts)
                obj.audit_conclusion = conclusion

            obj.save()
            if not obj.review_no:
                obj.review_no = next_serial(CustomerReview, 'review_no', 'CR')
                obj.save(update_fields=['review_no'])
            _enforce_source_snapshot(obj, request, original)
            log_operation(
                request, '客户评审', '编辑',
                f'[客户评审]下的记录#{obj.id}',
                f'编辑{obj.get_review_type_display()}：{obj.customer_name}',
                before=before, after=snapshot(obj),
            )
            messages.success(request, '已保存')
            return redirect('customer_review_detail', pk=obj.id)
    else:
        form = CustomerReviewForm(instance=obj)

    context = _review_form_context(
        review_type, obj, form,
        scores=obj.item_scores if isinstance(obj.item_scores, dict) else {},
        verdicts=obj.audit_verdicts if isinstance(obj.audit_verdicts, dict) else {},
    )
    return render(request, 'records/customer_review_form.html', context)


# ---------------------------------------------------------------------------
# 客户评审：详情 / 打印
# ---------------------------------------------------------------------------
def _review_detail_context(obj):
    groups, grades = _groups_for(obj.review_type)
    scores = obj.item_scores if isinstance(obj.item_scores, dict) else {}
    scores = {k: int(v) for k, v in scores.items() if isinstance(v, (int, float, str)) and str(v).lstrip('-').isdigit()}
    listed = spec.score_items(groups, scores) if groups else []
    ratio_rows = []
    if obj.review_type == CustomerReview.TYPE_CREDIT:
        # 已落库的比率优先；历史数据或后台直接录入的记录没有落库值，
        # 此时按基础数据现算一次，避免详情页满屏「—」却看不出原因。
        base = {f['key']: getattr(obj, f['key'], None) for f in spec.FIN_FIELDS}
        computed = spec.compute_ratios(base)
        for r in spec.RATIOS:
            value = getattr(obj, r['key'], None)
            if value is None:
                value = computed.get(r['key'])
            ratio_rows.append({
                **r,
                "display": spec.format_ratio(r['key'], float(value) if value is not None else None),
            })
    verdicts = obj.audit_verdicts if isinstance(obj.audit_verdicts, dict) else {}
    notes = obj.audit_notes if isinstance(obj.audit_notes, dict) else {}
    audit_rows = []
    if obj.review_type == CustomerReview.TYPE_AUDIT:
        for index, row in enumerate(spec.AUDIT_ROWS):
            key = f'r{index}'
            audit_rows.append({**row, 'key': key, 'verdict': verdicts.get(key, ''),
                               'note': notes.get(key, '')})
    return {
        'object': obj,
        'groups': listed,
        'group_rows': spec.group_scores(scores, groups) if groups else [],
        'grade_info': spec.grade_of(float(obj.total_score), grades) if grades else None,
        'ratio_rows': ratio_rows,
        'audit_rows': audit_rows,
        'audit_verdicts': verdicts,
        'total_possible': sum(g['max'] for g in (groups or [])),
    }


@perm_required(MODULE, 'read')
def review_detail(request, pk):
    obj = get_object_or_404(CustomerReview, pk=pk)
    context = _review_detail_context(obj)
    context.update(panel_context(request, obj))
    context['att_obj'] = obj
    return render(request, 'records/customer_review_detail.html', context)


@perm_required(MODULE, 'read')
def review_print(request, pk):
    obj = get_object_or_404(CustomerReview, pk=pk)
    context = _review_detail_context(obj)
    context['print_mode'] = True
    return render(request, 'records/customer_review_print.html', context)


customer_review_delete = make_delete_view(
    model=CustomerReview, perm_module=MODULE, label='客户评审',
    display_field='customer_name', redirect_name='customer_review_list',
    after_delete=lambda obj, obj_id: delete_for_object(obj, obj_id),
)


# ---------------------------------------------------------------------------
# 付款穿行测试
# ---------------------------------------------------------------------------
def _clean_terms(value):
    """账期天数：只接受 0～3650 的整数，其余存 None（表示继承表头默认）。"""
    days = spec.to_int(value, None)
    if days is None or days < 0 or days > 3650:
        return None
    return days


def _clean_date(value):
    """日期：能解析成 date 才落库，统一成 YYYY-MM-DD，避免脏字符串进 JSON。"""
    parsed = spec.parse_date(value)
    return parsed.isoformat() if parsed else ''


def _parse_samples(request):
    """解析样本 JSON，并按控制点定义做字段白名单收敛。

    不直接信任前端提交的对象：只保留定义过的键，避免脏数据进入
    samples 字段后让详情页/打印页渲染出无关内容。
    """
    raw = request.POST.get('samples_json', '')
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []

    step_keys = {s['key'] for s in spec.WALKTHROUGH_STEPS}
    results = {r[0] for r in spec.STEP_RESULTS}
    defects = set(spec.DEFECT_TYPES)

    clean = []
    for index, sample in enumerate(data, start=1):
        if not isinstance(sample, dict):
            continue
        steps = {}
        raw_steps = sample.get('steps') if isinstance(sample.get('steps'), dict) else {}
        for key, cell in raw_steps.items():
            if key not in step_keys or not isinstance(cell, dict):
                continue
            result = str(cell.get('result') or '').strip()
            defect = str(cell.get('defect') or '无').strip()
            steps[key] = {
                'result': result if result in results else '',
                'defect': defect if defect in defects else '无',
                'doc_no': str(cell.get('doc_no') or '')[:100],
                'doc_date': str(cell.get('doc_date') or '')[:20],
                'amount': str(cell.get('amount') or '')[:50],
                'note': str(cell.get('note') or '')[:500],
            }
        clean.append({
            'seq': index,
            'voucher_no': str(sample.get('voucher_no') or '')[:100],
            'pay_date': str(sample.get('pay_date') or '')[:20],
            'amount': str(sample.get('amount') or '')[:50],
            'payee': str(sample.get('payee') or '')[:100],
            # 账期核对：天数只收 0～3650，日期只收能解析的 ISO 文本，
            # 到期日与偏差由服务端按规则重算，不采信前端写好的结论
            'terms_days': _clean_terms(sample.get('terms_days')),
            'base_date': _clean_date(sample.get('base_date')),
            'due_date': _clean_date(sample.get('due_date')),
            'steps': steps,
        })
    return clean


def _approved_terms_for(payee_type, payee_name):
    """按客户名取最近一张填了批准账期的账期单（P3 实现细节约束 4）。

    仅客户对象适用；供应商或查不到时返回 None，表单回退现有手填行为。
    """
    if payee_type != '客户' or not payee_name:
        return None
    credit = (
        CustomerCredit.objects
        .filter(customer_name=payee_name, approved_terms_days__isnull=False)
        .order_by('-id')
        .first()
    )
    if credit is None:
        return None
    return {'days': credit.approved_terms_days,
            'no': credit.credit_no or f'#{credit.id}',
            'limit': credit.approved_limit}


def _walkthrough_context(instance, form, samples=None):
    samples = samples if samples is not None else (
        instance.samples if isinstance(instance.samples, list) else []
    )
    stats = spec.walkthrough_stats(samples, instance.terms_days)
    approval = _approved_terms_for(instance.payee_type, instance.payee_name)
    return {
        'form': form,
        'instance': instance,
        'steps': spec.WALKTHROUGH_STEPS,
        'step_results': spec.STEP_RESULTS,
        'defect_types': spec.DEFECT_TYPES,
        'samples': samples,
        'stats': stats,
        'approval': approval,
        'conclusions': spec.WALKTHROUGH_CONCLUSIONS,
        'sample_methods': spec.SAMPLE_METHODS,
        'terms_bases': spec.TERMS_BASE_CHOICES,
        'terms_grace': spec.TERMS_GRACE_DAYS,
        'key_steps': spec.WALKTHROUGH_KEY_STEPS,
        'is_edit': bool(instance and instance.pk),
    }


@perm_required(MODULE, 'create')
def walkthrough_add(request):
    instance = PaymentWalkthrough(
        test_date=timezone.localdate(),
        period_start=timezone.localdate().replace(month=1, day=1),
        period_end=timezone.localdate(),
    )
    if request.method == 'POST':
        form = PaymentWalkthroughForm(request.POST, instance=instance)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.created_by = request.user if request.user.is_authenticated else None
            obj.samples = _parse_samples(request)
            obj.conclusion = spec.walkthrough_stats(obj.samples, obj.terms_days)['conclusion'] or '无效'
            obj.sample_size = obj.sample_size or len(obj.samples)
            obj.save()
            log_operation(
                request, '付款穿行测试', '新增',
                f'[付款穿行测试]下的记录#{obj.id}',
                f'新增穿行测试：{obj.payee_name}（{obj.sample_size} 笔样本，结论 {obj.conclusion}）',
                after=snapshot(obj),
            )
            messages.success(request, '穿行测试已保存')
            return redirect('walkthrough_detail', pk=obj.id)
    else:
        form = PaymentWalkthroughForm(instance=instance)

    return render(request, 'records/walkthrough_form.html', _walkthrough_context(instance, form, []))


@perm_required(MODULE, 'update')
def walkthrough_edit(request, pk):
    obj = get_object_or_404(PaymentWalkthrough, pk=pk)
    if request.method == 'POST':
        form = PaymentWalkthroughForm(request.POST, instance=obj)
        if form.is_valid():
            before = snapshot(obj)
            obj = form.save(commit=False)
            obj.samples = _parse_samples(request)
            obj.conclusion = spec.walkthrough_stats(obj.samples, obj.terms_days)['conclusion'] or '无效'
            obj.sample_size = obj.sample_size or len(obj.samples)
            obj.save()
            log_operation(
                request, '付款穿行测试', '编辑',
                f'[付款穿行测试]下的记录#{obj.id}',
                f'编辑穿行测试：{obj.payee_name}（结论 {obj.conclusion}）',
                before=before, after=snapshot(obj),
            )
            messages.success(request, '已保存')
            return redirect('walkthrough_detail', pk=obj.id)
    else:
        form = PaymentWalkthroughForm(instance=obj)

    samples = obj.samples if isinstance(obj.samples, list) else []
    return render(request, 'records/walkthrough_form.html', _walkthrough_context(obj, form, samples))


def _sample_rows(samples, default_terms=None):
    """把样本 JSON 展开成模板可直接渲染的行结构。

    模板里无法用变量做字典取值（sample.steps[key]），所以在这里先把
    「控制点定义 + 该样本该环节的判定」配好对，模板只负责输出。
    账期核对同理：模板要显示到期日、偏差天数、按期判定，都在这里算好。
    """
    rows = []
    for index, sample in enumerate(samples, start=1):
        cells = sample.get('steps') if isinstance(sample.get('steps'), dict) else {}
        steps = []
        for step in spec.WALKTHROUGH_STEPS:
            cell = cells.get(step['key']) or {}
            steps.append({'meta': step, 'cell': cell,
                          'result': cell.get('result') or '',
                          'defect': cell.get('defect') or '无'})
        terms = spec.sample_terms(sample, default_terms)
        rows.append({
            'seq': sample.get('seq') or index,
            'voucher_no': sample.get('voucher_no') or '',
            'pay_date': sample.get('pay_date') or '',
            'amount': sample.get('amount') or '',
            'payee': sample.get('payee') or '',
            'steps': steps,
            'terms': {
                'terms_days': terms['terms_days'],
                'base_date': terms['base_date'].isoformat() if terms['base_date'] else '',
                'due_date': terms['due_date'].isoformat() if terms['due_date'] else '',
                'pay_date': terms['pay_date'].isoformat() if terms['pay_date'] else '',
                'delay_days': terms['delay_days'],
                'status': terms['status'],
                'tone': {'按期': 'success', '提前': 'info',
                         '逾期': 'danger'}.get(terms['status'], 'default'),
            },
        })
    return rows


@perm_required(MODULE, 'read')
def walkthrough_detail(request, pk):
    obj = get_object_or_404(PaymentWalkthrough, pk=pk)
    samples = obj.samples if isinstance(obj.samples, list) else []
    context = {
        'object': obj,
        'steps': spec.WALKTHROUGH_STEPS,
        'samples': samples,
        'sample_rows': _sample_rows(samples, obj.terms_days),
        'stats': spec.walkthrough_stats(samples, obj.terms_days),
        'key_steps': spec.WALKTHROUGH_KEY_STEPS,
        'terms_grace': spec.TERMS_GRACE_DAYS,
        'approval': _approved_terms_for(obj.payee_type, obj.payee_name),
        'att_obj': obj,
    }
    context.update(panel_context(request, obj))
    return render(request, 'records/walkthrough_detail.html', context)


@perm_required(MODULE, 'read')
def walkthrough_print(request, pk):
    obj = get_object_or_404(PaymentWalkthrough, pk=pk)
    samples = obj.samples if isinstance(obj.samples, list) else []
    context = {
        'object': obj,
        'steps': spec.WALKTHROUGH_STEPS,
        'samples': samples,
        'sample_rows': _sample_rows(samples, obj.terms_days),
        'stats': spec.walkthrough_stats(samples, obj.terms_days),
        'key_steps': spec.WALKTHROUGH_KEY_STEPS,
        'terms_grace': spec.TERMS_GRACE_DAYS,
        'approval': _approved_terms_for(obj.payee_type, obj.payee_name),
        'print_mode': True,
    }
    return render(request, 'records/walkthrough_print.html', context)


walkthrough_delete = make_delete_view(
    model=PaymentWalkthrough, perm_module=MODULE, label='付款穿行测试',
    display_field='payee_name', redirect_name='walkthrough_list',
    after_delete=lambda obj, obj_id: delete_for_object(obj, obj_id),
)


# ---------------------------------------------------------------------------
# 局部计算接口：前端录入财务数据时实时取比率
# ---------------------------------------------------------------------------
@perm_required(MODULE, 'read')
@require_POST
def ratio_preview(request):
    """返回比率 + 建议分。前端优先本地算，这个接口是兜底与口径校验。"""
    try:
        payload = json.loads(request.body or '{}')
    except ValueError:
        return HttpResponseBadRequest('请求体不是合法 JSON')
    if not isinstance(payload, dict):
        return HttpResponseBadRequest('请求体格式错误')

    base = {f['key']: payload.get(f['key']) for f in spec.FIN_FIELDS}
    ratios = spec.compute_ratios(base)
    return JsonResponse({
        'ratios': {
            key: spec.format_ratio(key, value) for key, value in ratios.items()
        },
        'raw': ratios,
        'suggest': spec.suggest_fin_scores(ratios),
    })


# ---------------------------------------------------------------------------
# Excel 导出 / 模板 / 导入
# ---------------------------------------------------------------------------
def _filtered_reviews(request):
    """按列表页同一组 GET 参数取数，保证「看到什么就导出什么」。"""
    qs = CustomerReview.objects.all()
    keyword = (request.GET.get('search') or '').strip()
    if keyword:
        qs = qs.filter(
            Q(customer_name__icontains=keyword)
            | Q(customer_code__icontains=keyword)
            | Q(review_no__icontains=keyword)
        )
    for param, lookup in (
        ('review_type', 'review_type'),
        ('customer_type', 'customer_type'),
        ('customer_name', 'customer_name__icontains'),
        ('grade', 'grade'),
    ):
        value = (request.GET.get(param) or '').strip()
        if value:
            qs = qs.filter(**{lookup: value})
    return qs


def _filtered_walkthroughs(request):
    qs = PaymentWalkthrough.objects.all()
    keyword = (request.GET.get('search') or '').strip()
    if keyword:
        qs = qs.filter(Q(payee_name__icontains=keyword) | Q(test_no__icontains=keyword))
    for param, lookup in (('payee_type', 'payee_type'), ('conclusion', 'conclusion')):
        value = (request.GET.get(param) or '').strip()
        if value:
            qs = qs.filter(**{lookup: value})
    return qs


@perm_required(MODULE, 'read')
@guarded_export
def review_export_list(request):
    """列表导出：一行一份表单，按类型分 sheet（列结构与导入模板一致）。"""
    qs = _filtered_reviews(request).order_by('review_type', '-review_date', '-id')
    total = qs.count()
    if not total:
        return HttpResponse('当前筛选条件下没有可导出的记录。',
                            content_type='text/plain; charset=utf-8')
    limit = max_export_rows()
    if total > limit:
        return export_too_large_response(total, limit)
    log_operation(request, '客户评审', '导出', '[客户评审]下的导出',
                  f'导出客户评审: {total} 条')
    return review_io.review_export_response(qs)


@perm_required(MODULE, 'read')
@guarded_export
def review_export_one(request, pk):
    """单份导出：还原纸质表单版式，供归档 / 发客户 / 打印。"""
    obj = get_object_or_404(CustomerReview, pk=pk)
    log_operation(request, '客户评审', '导出', f'[客户评审]下的记录#{obj.id}',
                  f'导出{obj.get_review_type_display()}：{obj.customer_name}')
    return review_io.review_form_response(obj)


@perm_required(MODULE, 'read')
def review_template(request):
    """下载导入模板：三张空表 + 填写说明。"""
    return review_io.review_template_response()


def _import_context(kind, sheet_names, template_url, action_url, list_url,
                    name_hint='表单类型', **extra):
    """导入页的公共 context：两种表共用一套模板，差异只有路由与提示文案。"""
    ctx = {
        'kind': kind,
        'sheet_names': sheet_names,
        'max_rows': review_io.MAX_IMPORT_ROWS,
        'template_url': template_url,
        'action_url': action_url,
        'list_url': list_url,
        'name_hint': name_hint,
    }
    ctx.update(extra)
    return ctx


def _review_import_context(**extra):
    return _import_context(
        '客户评审', list(review_io.SHEET_BY_TYPE.values()),
        'customer_review_template', 'customer_review_import',
        'customer_review_list',
        notes=[
            '三张表各占一个工作表，工作表名须为「信用评价 / 定期考评 / 现场审核」。',
            '必填列：客户全称、表单日期；「表单编号」非空时优先按编号匹配已有记录。',
            '合计得分、评定等级、审核结论由系统重算，Excel 中填的值不生效。',
            '评分列填分值（如 5 或「5 分」），超出档位时按最接近的合法档位收敛。',
            '默认仅补空；勾选覆盖后会替换已有非空字段。',
        ],
        **extra,
    )


def _walkthrough_import_context(**extra):
    return _import_context(
        '付款穿行测试', ['测试清单', '样本明细'],
        'walkthrough_template', 'walkthrough_import', 'walkthrough_list',
        name_hint='测试编号',
        notes=[
            '「测试清单」一行一个测试；「样本明细」一行 = 一个样本 × 一个控制点。',
            '两张表按「测试编号」关联，样本明细可以留空。',
            '测试结论由系统按样本执行情况重算，Excel 中填的值不生效。',
            '执行结果只接受：执行 / 部分执行 / 未执行 / 不适用。',
        ],
        **extra,
    )


@perm_required(MODULE, 'create')
def review_import(request):
    """导入第 1 步：上传 Excel → 预览（新增/更新/跳过/错误）。"""
    if request.method != 'POST':
        return render(request, 'records/customer_review_import.html',
                      _review_import_context())

    upload = request.FILES.get('file')
    overwrite = request.POST.get('overwrite') == '1'
    if not upload:
        return render(request, 'records/customer_review_import.html',
                      _review_import_context(error='请选择要导入的 Excel 文件'))
    try:
        sheets = review_io.parse_upload(upload)
        rows_by_type = review_io.split_import_sheets(sheets)
        if not rows_by_type:
            raise ValueError(
                '未识别到可导入的工作表：请使用系统模板（工作表名应为 '
                + ' / '.join(review_io.SHEET_BY_TYPE.values()) + '）'
            )
        preview = review_io.build_review_preview(rows_by_type, overwrite=overwrite)
        if not preview['items'] and not preview['errors']:
            raise ValueError('Excel 中没有可导入的数据行')
    except ValueError as exc:
        return render(request, 'records/customer_review_import.html',
                      _review_import_context(error=str(exc)))

    payload = json.dumps(rows_by_type, ensure_ascii=False)
    if len(payload.encode('utf-8')) > 2 * 1024 * 1024:
        return render(request, 'records/customer_review_import.html',
                      _review_import_context(
                          error='文件内容过大，无法一次性预览；请按表单类型拆成多个文件分批导入。'))
    return render(request, 'records/customer_review_import_preview.html', {
        'preview': preview,
        'payload': payload,
        'overwrite': overwrite,
        'filename': upload.name,
        'kind': '客户评审',
        'confirm_url': 'customer_review_import_confirm',
        'retry_url': 'customer_review_import',
        'list_url': 'customer_review_list',
        'with_samples': False,
    })


@perm_required(MODULE, 'create')
@require_POST
def review_import_confirm(request):
    """导入第 3 步：确认写入（单事务全成全败）。"""
    from accounts.services import has_perm

    try:
        rows_by_type = json.loads(request.POST.get('payload') or '{}')
        if not isinstance(rows_by_type, dict) or not rows_by_type:
            raise ValueError('导入数据已失效，请重新上传文件')
        overwrite = request.POST.get('overwrite') == '1'
        preview = review_io.build_review_preview(rows_by_type, overwrite=overwrite)
        if preview['errors']:
            raise ValueError('导入数据校验未通过：' + '；'.join(preview['errors'][:10]))
        if preview['summary']['update'] and not has_perm(request.user, MODULE, 'update'):
            raise ValueError('当前账号缺少编辑权限，无法更新已有记录')
        result = review_io.apply_review_rows(
            rows_by_type, overwrite=overwrite,
            user=request.user if request.user.is_authenticated else None,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return render(request, 'records/customer_review_import.html',
                      _review_import_context(error=str(exc)))

    log_operation(
        request, '客户评审', '导入', '[客户评审]下的导入',
        f'导入客户评审: 新增{result["created"]} 更新{result["updated"]} 跳过{result["skipped"]}',
    )
    return render(request, 'records/customer_review_import_result.html', {
        'result': result, 'kind': '客户评审', 'list_url': 'customer_review_list',
    })


@perm_required(MODULE, 'read')
@guarded_export
def walkthrough_export_list(request):
    """穿行测试列表导出：测试清单 + 样本明细两个工作表。"""
    qs = _filtered_walkthroughs(request).order_by('-test_date', '-id')
    total = qs.count()
    if not total:
        return HttpResponse('当前筛选条件下没有可导出的记录。',
                            content_type='text/plain; charset=utf-8')
    limit = max_export_rows()
    if total > limit:
        return export_too_large_response(total, limit)
    log_operation(request, '付款穿行测试', '导出', '[付款穿行测试]下的导出',
                  f'导出付款穿行测试: {total} 条')
    return review_io.walkthrough_export_response(qs)


@perm_required(MODULE, 'read')
@guarded_export
def walkthrough_export_one(request, pk):
    obj = get_object_or_404(PaymentWalkthrough, pk=pk)
    log_operation(request, '付款穿行测试', '导出', f'[付款穿行测试]下的记录#{obj.id}',
                  f'导出穿行测试：{obj.payee_name}')
    return review_io.walkthrough_form_response(obj)


@perm_required(MODULE, 'read')
def walkthrough_template(request):
    return review_io.walkthrough_template_response()


@perm_required(MODULE, 'create')
def walkthrough_import(request):
    if request.method != 'POST':
        return render(request, 'records/customer_review_import.html',
                      _walkthrough_import_context())

    upload = request.FILES.get('file')
    overwrite = request.POST.get('overwrite') == '1'
    if not upload:
        return render(request, 'records/customer_review_import.html',
                      _walkthrough_import_context(error='请选择要导入的 Excel 文件'))

    try:
        sheets = review_io.parse_upload(upload)
        sheets = {name: rows for name, rows in sheets.items() if rows}
        if review_io.build_walkthrough_import_preview(sheets)['summary']['total'] == 0:
            raise ValueError('未找到「测试清单」工作表或表内没有数据行，请使用系统模板')
    except ValueError as exc:
        return render(request, 'records/customer_review_import.html',
                      _walkthrough_import_context(error=str(exc)))

    payload = json.dumps({'sheets': sheets}, ensure_ascii=False)
    if len(payload.encode('utf-8')) > 2 * 1024 * 1024:
        return render(request, 'records/customer_review_import.html',
                      _walkthrough_import_context(
                          error='文件内容过大，无法一次性预览；请拆分后分批导入。'))

    return render(request, 'records/customer_review_import_preview.html', {
        'preview': review_io.build_walkthrough_import_preview(sheets, overwrite=overwrite),
        'payload': payload,
        'overwrite': overwrite,
        'filename': upload.name,
        'kind': '付款穿行测试',
        'confirm_url': 'walkthrough_import_confirm',
        'retry_url': 'walkthrough_import',
        'list_url': 'walkthrough_list',
        'with_samples': True,
    })


@perm_required(MODULE, 'create')
@require_POST
def walkthrough_import_confirm(request):
    from accounts.services import has_perm

    try:
        data = json.loads(request.POST.get('payload') or '{}')
        sheets = data.get('sheets') if isinstance(data, dict) else None
        if not isinstance(sheets, dict) or not sheets:
            raise ValueError('导入数据已失效，请重新上传文件')
        overwrite = request.POST.get('overwrite') == '1'
        preview = review_io.build_walkthrough_import_preview(sheets, overwrite=overwrite)
        if preview['errors']:
            raise ValueError('导入数据校验未通过：' + '；'.join(preview['errors'][:10]))
        if preview['summary']['update'] and not has_perm(request.user, MODULE, 'update'):
            raise ValueError('当前账号缺少编辑权限，无法更新已有记录')
        result = review_io.apply_walkthrough_import(
            sheets, overwrite=overwrite,
            user=request.user if request.user.is_authenticated else None,
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return render(request, 'records/customer_review_import.html',
                      _walkthrough_import_context(error=str(exc)))

    log_operation(
        request, '付款穿行测试', '导入', '[付款穿行测试]下的导入',
        f'导入穿行测试: 新增{result["created"]} 更新{result["updated"]} 跳过{result["skipped"]}',
    )
    return render(request, 'records/customer_review_import_result.html', {
        'result': result, 'kind': '付款穿行测试', 'list_url': 'walkthrough_list',
    })
