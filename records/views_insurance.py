"""保险聚合页面（阶段 1 只读）与续保录入（阶段 2）。

- 按标的聚合的续保列表；
- 单标的续保时间线；
- 应续未续 / 断档 / 即将到期报告；
- 续保视图：以上期保单为模板预填，保存后自动挂接标的/保单年度并留痕。
"""

from urllib.parse import quote

from django.core.paginator import Paginator
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from accounts.decorators import perm_required
from work_record.http_utils import per_page as _shared_per_page

from . import insurance_grouping as grouping
from . import insurance_services
from .audit import log_operation, snapshot
from .forms import InsuranceForm
from .models import Insurance


PER_PAGE_CHOICES = (10, 30, 50)


def _annotate_one_off(subjects):
    """给聚合结果补一次性标记，供模板展示与过滤。"""
    keys = insurance_services.one_off_keys()
    for subject in subjects:
        subject.is_one_off = subject.key in keys
    return subjects


def _per_page(request):
    return _shared_per_page(request, 30, choices=PER_PAGE_CHOICES)


def _all_records():
    return list(Insurance.objects.all())


@perm_required('insurance', 'read')
def insurance_subject_list(request):
    today = timezone.localdate()
    subjects = _annotate_one_off(
        grouping.build_subjects(_all_records(), today=today))
    summary = grouping.summarize(subjects)

    q = (request.GET.get('q') or '').strip()
    kind = request.GET.get('kind') or ''
    status = request.GET.get('status') or ''
    sort = request.GET.get('sort') or 'expiry'

    filtered = grouping.filter_subjects(subjects, q=q, kind=kind, status=status)
    if status == 'expired':
        filtered = [s for s in filtered if not s.is_one_off]
    filtered = grouping.sort_subjects(filtered, sort=sort)

    summary['expired'] = sum(
        1 for s in subjects if s.status == 'expired' and not s.is_one_off)

    paginator = Paginator(filtered, _per_page(request))
    page_obj = paginator.get_page(request.GET.get('page') or 1)

    return render(request, 'records/insurance_subject_list.html', {
        'subjects': page_obj,
        'page_obj': page_obj,
        'summary': summary,
        'q': q,
        'kind_filter': kind,
        'status_filter': status,
        'sort': sort,
        'per_page': _per_page(request),
    })


@perm_required('insurance', 'read')
def insurance_subject_detail(request):
    key = (request.GET.get('key') or '').strip()
    if not key:
        raise Http404('缺少保险标的参数')
    subjects = _annotate_one_off(
        grouping.build_subjects(_all_records(), today=timezone.localdate()))
    subject = next((s for s in subjects if s.key == key), None)
    if subject is None:
        raise Http404('保险标的不存在')
    return render(request, 'records/insurance_subject_detail.html', {
        'subject': subject,
    })


@perm_required('insurance', 'read')
def insurance_renewal_report(request):
    today = timezone.localdate()
    subjects = _annotate_one_off(
        grouping.build_subjects(_all_records(), today=today))
    report = grouping.build_report(subjects)
    summary = grouping.summarize(subjects)

    report['should_renew'] = [s for s in report['should_renew'] if not s.is_one_off]
    report['upcoming'] = [s for s in report['upcoming'] if not s.is_one_off]
    report['should_renew_count'] = len(report['should_renew'])
    report['upcoming_count'] = len(report['upcoming'])
    summary['expired'] = sum(
        1 for s in subjects if s.status == 'expired' and not s.is_one_off)

    return render(request, 'records/insurance_renewal_report.html', {
        'report': report,
        'summary': summary,
    })


@perm_required('insurance', 'update')
def insurance_subject_one_off(request):
    """把标的标记为一次性保险 / 恢复续保监控。

    一次性标的不会再出现在应续未续、临期报告与定时提醒中；
    标记与恢复均写操作留痕，便于回溯误操作。
    """
    if request.method != 'POST':
        raise Http404('仅支持 POST')
    key = (request.POST.get('key') or '').strip()
    subject = insurance_services.subject_by_key(key)
    if subject is None:
        raise Http404('保险标的不存在')

    action = request.POST.get('action')
    if action not in ('mark', 'unmark'):
        raise Http404('未知操作')
    subject.is_one_off = action == 'mark'
    subject.save(update_fields=['is_one_off', 'updated_at'])
    log_operation(
        request, '保险',
        '标记一次性' if subject.is_one_off else '恢复续保监控',
        f'保险标的#{subject.id}',
        f'{subject.label}：{"不再续保，停止提醒" if subject.is_one_off else "恢复续保提醒"}',
    )

    if request.POST.get('next') == 'detail':
        return redirect(f"{reverse('insurance_subject_detail')}?key={quote(key)}")
    return redirect('insurance_renewal_report')


@perm_required('insurance', 'create')
def insurance_renew(request):
    """续保录入：以上期保单为模板预填，保存后自动挂接标的/保单年度。

    GET  参数 from_record=<上期保单id>：预填保司/标题/部门/保费，
         日期顺推一年（上期截止日 +1 天 ~ +1 年），单号清空待填；
    POST 创建新保单记录，attach_record 挂接，写「续保」留痕；
    整页表单 + AJAX 弹窗双模式（与新增/编辑一致）。
    """
    is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'

    def _modal(form, ok):
        html = render_to_string('records/_form_modal.html',
                                {'form': form, 'save_label': '保存续保'},
                                request=request)
        return JsonResponse({'ok': ok, 'html': html}, status=200 if ok else 422)

    if request.method == 'POST':
        form = InsuranceForm(request.POST)
        if form.is_valid():
            obj = form.save()
            insurance_services.attach_record_safe(obj)
            log_operation(request, '保险', '续保', f'[保险]下的记录#{obj.id}',
                          f'续保保险: {obj.company_name} {obj.title}',
                          after=snapshot(obj))
            if is_ajax:
                return JsonResponse({'ok': True, 'message': '续保成功'})
            return redirect('insurance_subject_list')
        if is_ajax:
            return _modal(form, False)
    else:
        from_record_id = request.GET.get('from_record')
        if not from_record_id:
            raise Http404('缺少上期保单参数')
        prev = get_object_or_404(Insurance, pk=from_record_id)
        form = InsuranceForm(initial=insurance_services.renewal_initial(prev))

    if is_ajax:
        return _modal(form, True)
    return render(request, 'records/insurance_form.html', {
        'form': form, 'title': '续保保险',
    })
