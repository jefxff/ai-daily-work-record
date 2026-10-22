"""保险只读聚合页面（阶段 1）。

只读视图，不新增模型、不改数据库：
- 按标的聚合的续保列表；
- 单标的续保时间线；
- 应续未续 / 断档 / 即将到期报告。
"""

from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import render
from django.utils import timezone

from accounts.decorators import perm_required

from . import insurance_grouping as grouping
from .models import Insurance


PER_PAGE_CHOICES = (10, 30, 50)


def _per_page(request):
    try:
        value = int(request.GET.get('per_page', 30))
    except (TypeError, ValueError):
        value = 30
    return value if value in PER_PAGE_CHOICES else 30


def _all_records():
    return list(Insurance.objects.all())


@perm_required('insurance', 'read')
def insurance_subject_list(request):
    today = timezone.localdate()
    subjects = grouping.build_subjects(_all_records(), today=today)
    summary = grouping.summarize(subjects)

    q = (request.GET.get('q') or '').strip()
    kind = request.GET.get('kind') or ''
    status = request.GET.get('status') or ''
    sort = request.GET.get('sort') or 'expiry'

    filtered = grouping.filter_subjects(subjects, q=q, kind=kind, status=status)
    filtered = grouping.sort_subjects(filtered, sort=sort)

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
    subjects = grouping.build_subjects(_all_records(), today=timezone.localdate())
    subject = next((s for s in subjects if s.key == key), None)
    if subject is None:
        raise Http404('保险标的不存在')
    return render(request, 'records/insurance_subject_detail.html', {
        'subject': subject,
    })


@perm_required('insurance', 'read')
def insurance_renewal_report(request):
    today = timezone.localdate()
    subjects = grouping.build_subjects(_all_records(), today=today)
    report = grouping.build_report(subjects)
    summary = grouping.summarize(subjects)
    return render(request, 'records/insurance_renewal_report.html', {
        'report': report,
        'summary': summary,
    })
