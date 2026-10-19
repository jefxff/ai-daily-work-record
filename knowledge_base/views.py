import markdown
from datetime import timedelta
from django.http import JsonResponse
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


def _is_ajax(request):
    return request.headers.get('x-requested-with') == 'XMLHttpRequest'


def _modal_form(request, template, context, *, ok=True):
    """渲染弹窗局部表单：ok=True(200) 初始加载，ok=False(422) 校验失败。"""
    html = render_to_string(template, context, request=request)
    return JsonResponse({'ok': ok, 'html': html}, status=200 if ok else 422)


@perm_required("knowledge", "read")
def document_list(request):
    """文档列表"""
    category_id = request.GET.get('category')
    tag_filter = request.GET.get('tag')
    documents = KnowledgeDocument.objects.filter(status='published')
    
    if category_id:
        documents = documents.filter(category_id=category_id)
    
    if tag_filter:
        documents = documents.filter(tags__icontains=tag_filter)
    
    categories = KnowledgeCategory.objects.all()
    
    # 统计数据（聚合下推到数据库，避免把整表对象加载进 Python 求和）
    total_documents = KnowledgeDocument.objects.filter(status='published').count()
    total_categories = KnowledgeCategory.objects.count()
    total_views = KnowledgeDocument.objects.aggregate(total=Sum('view_count'))['total'] or 0
    avg_views = total_views // total_documents if total_documents > 0 else 0
    
    # 获取所有标签并统计数量（支持中文逗号和英文逗号分隔）
    # 仅取 tags 单列（values_list），避免全表对象实例化
    all_tags = {}
    for tags_text in KnowledgeDocument.objects.filter(status='published').values_list('tags', flat=True):
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


@perm_required("knowledge", "read")
def document_detail(request, pk):
    """文档详情"""
    document = get_object_or_404(KnowledgeDocument, pk=pk)
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

    # 获取相关文档（同分类或同标签）
    related_docs = KnowledgeDocument.objects.filter(
        Q(category=document.category) | Q(tags__icontains=document.tags.split(',')[0] if document.tags else '')
    ).exclude(pk=pk).distinct()[:5]

    context = {
        'document': document,
        'related_docs': related_docs,
    }
    if is_ajax:
        return _modal_form(request, 'knowledge_base/_document_detail_modal.html', context)
    return render(request, 'knowledge_base/document_detail.html', context)


@perm_required("knowledge", "create")
def document_create(request):
    """创建文档"""
    is_ajax = _is_ajax(request)
    if request.method == 'POST':
        form = KnowledgeDocumentForm(request.POST)
        if form.is_valid():
            doc = form.save()
            if is_ajax:
                return JsonResponse({'ok': True, 'message': f'文档「{doc.title}」创建成功'})
            return redirect('knowledge_base:document_list')
        if is_ajax:
            return _modal_form(request, 'knowledge_base/_document_form_modal.html', {'form': form}, ok=False)
    else:
        form = KnowledgeDocumentForm()

    if is_ajax:
        return _modal_form(request, 'knowledge_base/_document_form_modal.html', {'form': form})
    context = {
        'form': form,
        'title': '创建文档',
    }
    return render(request, 'knowledge_base/document_form.html', context)


@perm_required("knowledge", "update")
def document_edit(request, pk):
    """编辑文档"""
    document = get_object_or_404(KnowledgeDocument, pk=pk)
    is_ajax = _is_ajax(request)

    if request.method == 'POST':
        form = KnowledgeDocumentForm(request.POST, instance=document)
        if form.is_valid():
            form.save()
            if is_ajax:
                return JsonResponse({'ok': True, 'message': '文档已更新'})
            return redirect('knowledge_base:document_detail', pk=pk)
        if is_ajax:
            return _modal_form(request, 'knowledge_base/_document_form_modal.html',
                               {'form': form, 'document': document}, ok=False)
    else:
        form = KnowledgeDocumentForm(instance=document)

    if is_ajax:
        return _modal_form(request, 'knowledge_base/_document_form_modal.html',
                           {'form': form, 'document': document})
    context = {
        'form': form,
        'title': '编辑文档',
        'document': document,
    }
    return render(request, 'knowledge_base/document_form.html', context)


@perm_required("knowledge", "delete")
def document_delete(request, pk):
    """删除文档"""
    document = get_object_or_404(KnowledgeDocument, pk=pk)
    
    if request.method == 'POST':
        document.delete()
        return redirect('knowledge_base:document_list')
    
    context = {
        'document': document,
    }
    return render(request, 'knowledge_base/document_delete.html', context)


@perm_required("knowledge", "read")
def document_search(request):
    """文档搜索"""
    keyword = request.GET.get('keyword', '')
    if keyword:
        documents = KnowledgeDocument.objects.filter(
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
    qs = KnowledgeView.objects.filter(viewed_at__gte=since)

    total_views = qs.count()
    unique_viewers = qs.exclude(viewer='').values('viewer').distinct().count()
    viewed_docs = qs.values('document_id').distinct().count()
    published_docs = KnowledgeDocument.objects.filter(status='published').count()
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
