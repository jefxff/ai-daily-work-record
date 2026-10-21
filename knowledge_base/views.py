import markdown
from datetime import timedelta
from django.http import JsonResponse, HttpResponseForbidden
from django.shortcuts import render, get_object_or_404, redirect
from django.core.paginator import Paginator
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import TruncDate
from django.template.loader import render_to_string
from django.utils import timezone
from records.crud import _per_page
from .models import KnowledgeDocument, KnowledgeCategory, KnowledgeView
from .forms import KnowledgeDocumentForm, KnowledgeCategoryForm
from accounts.decorators import perm_required
from accounts.security import client_ip
from . import access


def _is_ajax(request):
    return request.headers.get('x-requested-with') == 'XMLHttpRequest'


def _modal_form(request, template, context, *, ok=True):
    """渲染弹窗局部表单：ok=True(200) 初始加载，ok=False(422) 校验失败。"""
    html = render_to_string(template, context, request=request)
    return JsonResponse({'ok': ok, 'html': html}, status=200 if ok else 422)


@perm_required("knowledge", "read")
def document_list(request):
    """文档列表（读路径改造：所有统计/标签/分类计数只算当前用户可见范围）"""
    category_id = request.GET.get('category')
    tag_filter = request.GET.get('tag')
    # 唯一入口 access.visible_docs：公开 + 本人文档；漏一处就是泄漏口
    documents = access.visible_docs(request.user).filter(status='published')

    if category_id:
        documents = documents.filter(category_id=category_id)

    if tag_filter:
        documents = documents.filter(tags__icontains=tag_filter)

    # 分类本身不隐藏（分类名不算敏感信息，全隐藏会导致新建文档选不到分类），
    # 但计数只算可见的，否则「分类 42 篇」点进去只有 3 条，用户以为系统坏了
    published_visible_q = Q(documents__status='published') & (
        Q(documents__visibility=access.PUBLIC) | Q(documents__owner=request.user))
    if access.can_read_private_of_others(request.user):
        categories = KnowledgeCategory.objects.annotate(
            doc_count=Count('documents', distinct=True))
    else:
        categories = KnowledgeCategory.objects.annotate(
            doc_count=Count('documents', filter=published_visible_q, distinct=True))

    # 统计数据（聚合下推到数据库，避免把整表对象加载进 Python 求和）
    visible = access.visible_docs(request.user).filter(status='published')
    total_documents = visible.count()
    total_categories = KnowledgeCategory.objects.count()
    total_views = visible.aggregate(total=Sum('view_count'))['total'] or 0
    avg_views = total_views // total_documents if total_documents > 0 else 0

    # 获取所有标签并统计数量（支持中文逗号和英文逗号分隔）
    # 仅取 tags 单列（values_list），避免全表对象实例化；口径=可见文档
    all_tags = {}
    for tags_text in visible.values_list('tags', flat=True):
        if tags_text:
            # 先按中文逗号拆分，再按英文逗号拆分
            tags = []
            parts = tags_text.split('、')  # 中文逗号
            for part in parts:
                sub_tags = [t.strip() for t in part.split(',') if t.strip()]
                tags.extend(sub_tags)
            for tag in tags:
                all_tags[tag] = all_tags.get(tag, 0) + 1
    
    # 为标签添加颜色和字体大小
    tag_colors = [
        'rgba(155, 89, 182, 0.9)',
        'rgba(52, 152, 219, 0.9)',
        'rgba(46, 204, 113, 0.9)',
        'rgba(241, 196, 15, 0.9)',
        'rgba(231, 76, 60, 0.9)',
        'rgba(255, 165, 0, 0.9)',
        'rgba(149, 165, 166, 0.9)',
        'rgba(230, 126, 34, 0.9)',
    ]
    
    tag_list = []
    for i, (tag, count) in enumerate(all_tags.items()):
        color = tag_colors[i % len(tag_colors)]
        if count >= 5:
            font_size = '1.3rem'
        elif count >= 4:
            font_size = '1.2rem'
        elif count >= 3:
            font_size = '1.1rem'
        elif count >= 2:
            font_size = '1rem'
        else:
            font_size = '0.9rem'
        tag_list.append({
            'name': tag,
            'count': count,
            'color': color,
            'font_size': font_size,
        })
    
    # 后端分页（沿用 records.crud._per_page：10/30/50）
    per_page = _per_page(request)
    page_obj = Paginator(documents, per_page).get_page(request.GET.get('page'))

    context = {
        'documents': page_obj,
        'page_obj': page_obj,
        'per_page': per_page,
        'categories': categories,
        'selected_category': category_id,
        'selected_tag': tag_filter,
        'total_documents': total_documents,
        'total_categories': total_categories,
        'total_views': total_views,
        'avg_views': avg_views,
        'tag_list': tag_list,
    }
    return render(request, 'knowledge_base/document_list.html', context)


# —— XSS 防护：markdown 转换结果经 bleach 白名单过滤后才交给模板 |safe 渲染 ——
_ALLOWED_TAGS = [
    "p", "br", "strong", "em", "u", "s", "del", "ins", "h1", "h2", "h3", "h4",
    "h5", "h6", "ul", "ol", "li", "blockquote", "code", "pre", "hr",
    "table", "thead", "tbody", "tr", "th", "td",
    "a", "img", "span", "div", "sup", "sub",
]
_ALLOWED_ATTRS = {
    "a": ["href", "title", "target", "rel"],
    "img": ["src", "alt", "title", "width", "height"],
    "th": ["align"], "td": ["align"],
    "span": ["class"], "div": ["class"], "code": ["class"], "pre": ["class"],
}


def _sanitize_html(html: str) -> str:
    import bleach

    return bleach.clean(
        html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS,
        protocols=["http", "https", "mailto"], strip=True,
    )


def _vis_form_context(document=None, user=None):
    """表单/详情模板共用的可见性上下文（单选框 + 徽章 + 切换按钮全用它）。"""
    return {
        'visibility_choices': access.visibility_choices_for_ui(),
        'current_visibility': document.visibility if document is not None else access.PRIVATE,
        'can_manage_vis': True if document is None else access.can_manage_visibility(document, user),
        'visibility_label': access.visibility_label(document),
    }


@perm_required("knowledge", "read")
def document_detail(request, pk):
    """文档详情（无权访问一律 404：403 等于告诉对方「这篇存在，只是你没权限」）"""
    document = get_object_or_404(access.visible_docs(request.user), pk=pk)
    is_ajax = _is_ajax(request)
    if not is_ajax:
        # 原子自增（F 表达式），避免并发浏览时的读-改-写覆盖导致计数丢失
        KnowledgeDocument.objects.filter(pk=document.pk).update(
            view_count=F('view_count') + 1)
        document.refresh_from_db(fields=['view_count'])
        # 阅读明细表：每次打开详情页写一行（弹窗预览不计入，避免重复计数）
        KnowledgeView.objects.create(
            document=document,
            user=request.user if request.user.is_authenticated else None,
            viewer=request.user.get_username() if request.user.is_authenticated else '',
            ip=client_ip(request),
        )

    # 将Markdown内容转换为HTML，并过滤 script/iframe/事件属性等危险内容
    document.content_html = _sanitize_html(markdown.markdown(
        document.content,
        extensions=[
            'markdown.extensions.extra',
            'markdown.extensions.codehilite',
            'markdown.extensions.toc',
        ]
    ))

    # 获取相关文档（同分类或同标签；同样只从可见范围里挑，
    # 否则会通过「相关文档」把私有文档的标题带出来）
    related_docs = access.visible_docs(request.user).filter(
        Q(category=document.category) | Q(tags__icontains=document.tags.split(',')[0] if document.tags else '')
    ).exclude(pk=pk).distinct()[:5]

    context = {
        'document': document,
        'related_docs': related_docs,
        **_vis_form_context(document, request.user),
    }
    if is_ajax:
        return _modal_form(request, 'knowledge_base/_document_detail_modal.html', context)
    return render(request, 'knowledge_base/document_detail.html', context)


@perm_required("knowledge", "create")
def document_create(request):
    """创建文档（写入时定所有权：owner 为空的文档连创建人自己都改不了）"""
    is_ajax = _is_ajax(request)
    if request.method == 'POST':
        form = KnowledgeDocumentForm(request.POST)
        if form.is_valid():
            doc = form.save(commit=False)
            # 可见性：表单单选框（批次 2 上 UI）；接口先落地，默认/非法值一律私有
            target_visibility = request.POST.get('visibility')
            doc.visibility = (target_visibility if target_visibility in access.VISIBILITY_LABELS
                              else access.PRIVATE)
            access.ensure_owner(doc, request.user)
            doc.save()
            if is_ajax:
                return JsonResponse({'ok': True, 'message': f'文档「{doc.title}」创建成功'})
            return redirect('knowledge_base:document_list')
        if is_ajax:
            return _modal_form(request, 'knowledge_base/_document_form_modal.html',
                               {'form': form, **_vis_form_context(None, request.user)}, ok=False)
    else:
        form = KnowledgeDocumentForm()

    if is_ajax:
        return _modal_form(request, 'knowledge_base/_document_form_modal.html',
                           {'form': form, **_vis_form_context(None, request.user)})
    context = {
        'form': form,
        'title': '创建文档',
        **_vis_form_context(None, request.user),
    }
    return render(request, 'knowledge_base/document_form.html', context)


@perm_required("knowledge", "update")
def document_edit(request, pk):
    """编辑文档（owner 专属：公开文档他人只读；他人私有文档直接 404）"""
    document = get_object_or_404(access.visible_docs(request.user), pk=pk)
    if not access.can_edit(document, request.user):
        message = '只能编辑自己创建的文档'
        if _is_ajax(request):
            return JsonResponse({'ok': False, 'message': message}, status=403)
        return HttpResponseForbidden(message)
    is_ajax = _is_ajax(request)

    if request.method == 'POST':
        form = KnowledgeDocumentForm(request.POST, instance=document)
        if form.is_valid():
            form.save()
            # 可见性单选框（批次 2 上 UI）：切换走唯一入口并留痕
            target = request.POST.get('visibility')
            if target in access.VISIBILITY_LABELS and access.can_manage_visibility(document, request.user):
                access.set_visibility(document, target, request=request, source='编辑表单')
            if is_ajax:
                return JsonResponse({'ok': True, 'message': '文档已更新'})
            return redirect('knowledge_base:document_detail', pk=pk)
        if is_ajax:
            return _modal_form(request, 'knowledge_base/_document_form_modal.html',
                               {'form': form, 'document': document,
                                **_vis_form_context(document, request.user)}, ok=False)
    else:
        form = KnowledgeDocumentForm(instance=document)

    if is_ajax:
        return _modal_form(request, 'knowledge_base/_document_form_modal.html',
                           {'form': form, 'document': document,
                            **_vis_form_context(document, request.user)})
    context = {
        'form': form,
        'title': '编辑文档',
        'document': document,
        **_vis_form_context(document, request.user),
    }
    return render(request, 'knowledge_base/document_form.html', context)


@perm_required("knowledge", "update")
def document_visibility(request, pk):
    """切换可见性（owner 专属；仅 POST；切换全程留痕）。"""
    if request.method != 'POST':
        return JsonResponse({'ok': False, 'message': '仅支持 POST 请求'}, status=405)
    document = get_object_or_404(access.visible_docs(request.user), pk=pk)
    if not access.can_manage_visibility(document, request.user):
        return JsonResponse({'ok': False, 'message': '只有创建人可以修改可见性'}, status=403)
    target = request.POST.get('visibility')
    if target not in access.VISIBILITY_LABELS:
        return JsonResponse({'ok': False, 'message': '非法的可见性取值'}, status=400)

    changed = access.set_visibility(document, target, request=request, source='详情页切换')
    if changed:
        message = ('已设为全员可见，所有有知识库权限的同事现在可以阅读'
                   if target == access.PUBLIC else '已收回为仅自己可见')
    else:
        message = '可见性未变化'
    return JsonResponse({
        'ok': True, 'message': message,
        'visibility': document.visibility,
        'visibility_label': access.visibility_label(document),
    })


@perm_required("knowledge", "delete")
def document_delete(request, pk):
    """删除文档（owner 专属；否则任何有 knowledge.delete 的人都能删掉别人的私人笔记）"""
    document = get_object_or_404(access.visible_docs(request.user), pk=pk)
    if not access.can_delete(document, request.user):
        message = '只能删除自己创建的文档'
        if _is_ajax(request):
            return JsonResponse({'ok': False, 'message': message}, status=403)
        return HttpResponseForbidden(message)

    if request.method == 'POST':
        document.delete()
        return redirect('knowledge_base:document_list')

    context = {
        'document': document,
    }
    return render(request, 'knowledge_base/document_delete.html', context)


@perm_required("knowledge", "read")
def document_search(request):
    """文档搜索（搜索会命中正文，正文里有客户名、金额、人名——必须过滤）"""
    keyword = request.GET.get('keyword', '')
    if keyword:
        documents = access.visible_docs(request.user).filter(
            Q(title__icontains=keyword) |
            Q(content__icontains=keyword) |
            Q(tags__icontains=keyword)
        ).filter(status='published')
    else:
        documents = KnowledgeDocument.objects.none()

    per_page = _per_page(request)
    page_obj = Paginator(documents, per_page).get_page(request.GET.get('page'))

    context = {
        'documents': page_obj,
        'page_obj': page_obj,
        'per_page': per_page,
        'keyword': keyword,
    }
    return render(request, 'knowledge_base/document_search.html', context)


@perm_required("knowledge", "read")
def category_list(request):
    """分类列表"""
    categories = KnowledgeCategory.objects.all()
    context = {
        'categories': categories,
    }
    return render(request, 'knowledge_base/category_list.html', context)


@perm_required("knowledge", "create")
def category_create(request):
    """创建分类"""
    is_ajax = _is_ajax(request)
    if request.method == 'POST':
        form = KnowledgeCategoryForm(request.POST)
        if form.is_valid():
            cat = form.save()
            if is_ajax:
                return JsonResponse({'ok': True, 'message': f'分类「{cat.name}」创建成功'})
            return redirect('knowledge_base:category_list')
        if is_ajax:
            return _modal_form(request, 'knowledge_base/_category_form_modal.html', {'form': form}, ok=False)
    else:
        form = KnowledgeCategoryForm()

    if is_ajax:
        return _modal_form(request, 'knowledge_base/_category_form_modal.html', {'form': form})
    context = {
        'form': form,
        'title': '创建分类',
    }
    return render(request, 'knowledge_base/category_form.html', context)


@perm_required("knowledge", "update")
def category_edit(request, pk):
    """编辑分类"""
    category = get_object_or_404(KnowledgeCategory, pk=pk)
    is_ajax = _is_ajax(request)

    if request.method == 'POST':
        form = KnowledgeCategoryForm(request.POST, instance=category)
        if form.is_valid():
            form.save()
            if is_ajax:
                return JsonResponse({'ok': True, 'message': '分类已更新'})
            return redirect('knowledge_base:category_list')
        if is_ajax:
            return _modal_form(request, 'knowledge_base/_category_form_modal.html',
                               {'form': form, 'category': category}, ok=False)
    else:
        form = KnowledgeCategoryForm(instance=category)

    if is_ajax:
        return _modal_form(request, 'knowledge_base/_category_form_modal.html',
                           {'form': form, 'category': category})
    context = {
        'form': form,
        'title': '编辑分类',
        'category': category,
    }
    return render(request, 'knowledge_base/category_form.html', context)


@perm_required("knowledge", "delete")
def category_delete(request, pk):
    """删除分类"""
    category = get_object_or_404(KnowledgeCategory, pk=pk)
    
    if request.method == 'POST':
        category.delete()
        return redirect('knowledge_base:category_list')
    
    context = {
        'category': category,
        'document_count': category.documents.count(),
    }
    return render(request, 'knowledge_base/category_delete.html', context)


# —— 阅读报告（系统级统计页：知识库模块的读取分析，仅 system.read 可访问）——

REPORT_DEFAULT_DAYS = 30
REPORT_MIN_DAYS = 1
REPORT_MAX_DAYS = 365
REPORT_TOP_N = 10


def _clamp_report_days(raw):
    """解析 GET ``days``，非法值回退默认、越界夹取到 [MIN, MAX]。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return REPORT_DEFAULT_DAYS
    return max(REPORT_MIN_DAYS, min(REPORT_MAX_DAYS, value))


@perm_required("system", "read")
def knowledge_report(request):
    """知识库阅读报告：窗口内的阅读总量 / 独立阅读人 / 覆盖文档 / 榜单 / 明细。

    数据源为 ``KnowledgeView``（每次详情页浏览一行）。页面本身不写阅读记录。
    """
    days = _clamp_report_days(request.GET.get('days'))
    since = timezone.now() - timedelta(days=days)
    # 保守口径：榜单只统计公开文档 + 本人文档，不把他人私有文档标题摆出来
    # （「看不到」一旦破了例，后面每加一个报表都要重新讨论一次）
    qs = KnowledgeView.objects.filter(viewed_at__gte=since).filter(
        access.doc_visibility_q(request.user, 'document__'))

    total_views = qs.count()
    unique_viewers = qs.exclude(viewer='').values('viewer').distinct().count()
    viewed_docs = qs.values('document_id').distinct().count()
    published_docs = access.visible_docs(request.user).filter(status='published').count()
    unread_docs = max(published_docs - viewed_docs, 0)

    # 榜单：阅读次数最多的文档 / 阅读最活跃的人
    top_documents = list(
        qs.values('document_id', 'document__title')
        .annotate(cnt=Count('id'))
        .order_by('-cnt')[:REPORT_TOP_N]
    )
    top_readers = list(
        qs.exclude(viewer='').values('viewer')
        .annotate(cnt=Count('id'))
        .order_by('-cnt')[:REPORT_TOP_N]
    )
    # 每日趋势（仅返回窗口内有阅读的日期）
    daily = list(
        qs.annotate(day=TruncDate('viewed_at'))
        .values('day')
        .annotate(cnt=Count('id'))
        .order_by('day')
    )

    per_page = _per_page(request)
    page_obj = Paginator(
        qs.select_related('document'), per_page
    ).get_page(request.GET.get('page'))

    context = {
        'days': days,
        'since': since,
        'total_views': total_views,
        'unique_viewers': unique_viewers,
        'viewed_docs': viewed_docs,
        'published_docs': published_docs,
        'unread_docs': unread_docs,
        'top_documents': top_documents,
        'top_readers': top_readers,
        'daily': daily,
        'page_obj': page_obj,
        'per_page': per_page,
    }
    return render(request, 'knowledge_base/knowledge_report.html', context)
