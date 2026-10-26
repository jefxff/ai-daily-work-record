"""全站搜索视图（从 records/views.py 拆出，复核报告 ARC-03）。"""

from django.db.models import Q
from django.shortcuts import render

from accounts.decorators import require_any_module
from accounts.services import visible_modules
from archive.models import Archive
from knowledge_base.models import KnowledgeDocument
from samples.models import Partner, Sample

from .models import (
    Complaint,
    CustomerComplaint,
    CustomerCredit,
    CustomerLedger,
    Decoration,
    Insurance,
    MetalPrice,
    PriceEvaluation,
    Regulation,
)
from .view_common import _my_daily_records

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

    # 搜索客户台账
    customer_ledger_results = CustomerLedger.objects.filter(
        Q(customer_name__icontains=search_term) |
        Q(responsible_salesperson__icontains=search_term) |
        Q(legal_representative__icontains=search_term) |
        Q(credit_code__icontains=search_term) |
        Q(address__icontains=search_term) |
        Q(archive_no__icontains=search_term) |
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
        'customer_ledger': ('customer_ledger_results', customer_ledger_results),
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
