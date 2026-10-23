import json
import re
import uuid
from datetime import date, timedelta

import markdown
from django.http import JsonResponse, HttpResponseForbidden
from django.shortcuts import render, get_object_or_404, redirect
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import TruncDate
from django.template.loader import render_to_string
from django.utils import timezone
from records.audit import log_operation, snapshot
from records.crud import _per_page
from .models import KnowledgeDocument, KnowledgeCategory, KnowledgeNode, KnowledgeView
from .forms import KnowledgeDocumentForm, KnowledgeCategoryForm
from accounts.decorators import perm_required
from accounts.security import client_ip
from accounts.services import has_perm
from . import access
from . import folders as kb_folders
from . import outline as kb_outline
from work_record.http_utils import is_ajax as _is_ajax  # 统一实现（复核报告 CODE-01）


def _int_or_none(value):
    """GET 参数 → int；非数字返回 None（绝不让脏参数击穿过滤链）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _modal_form(request, template, context, *, ok=True):
    """渲染弹窗局部表单：ok=True(200) 初始加载，ok=False(422) 校验失败。"""
    html = render_to_string(template, context, request=request)
    return JsonResponse({'ok': ok, 'html': html}, status=200 if ok else 422)


def _sidebar_context(user, selected_category_id=None, selected_tag=None, current_doc_id=None):
    """右栏上下文（列表页与详情页共用，见 _right_sidebar.html）。

    包含：统计四联（文档/分类/浏览/平均）、分类树（含可见口径计数与
    选中项的祖先展开链）、标签云。分类计数只算当前用户可见的已发布文档——
    分类名不算敏感信息故分类本身不隐藏，但计数口径必须与列表一致，
    否则「分类 42 篇」点进去只有 3 条，用户以为系统坏了。

    每个分类还挂载其下可见的已发布文档（``docs``）：右栏分类树的 caret
    展开即「文件夹系统」——子分类与文档都在展开区里。文档标题只在可见
    范围内出现（他人私有文档不进列表，与计数/列表同口径；自己的私有
    文档带锁标）。``current_doc_id`` 供详情页在树里高亮当前文档。
    """
    published_visible_q = Q(documents__status='published') & (
        Q(documents__visibility=access.PUBLIC) | Q(documents__owner=user))
    published_q = Q(documents__status='published')
    if access.can_read_private_of_others(user):
        categories = KnowledgeCategory.objects.annotate(
            doc_count=Count('documents', filter=published_q, distinct=True))
    else:
        categories = KnowledgeCategory.objects.annotate(
            doc_count=Count('documents', filter=published_visible_q, distinct=True))

    # 分类 → 可见文档（与计数同口径：已发布 + 可见范围）。标题是用户本来
    # 就能在列表页看到的信息，不存在新增泄漏面；一次查询按分类分组，
    # 避免 N+1。
    docs_by_cat = {}
    visible_published = (access.visible_docs(user)
                         .filter(status='published')
                         .only('id', 'title', 'visibility', 'category_id')
                         .order_by('-updated_at'))
    for d in visible_published:
        docs_by_cat.setdefault(d.category_id, []).append(d)

    # 分类树：parent 自引用组装（列表页筛选用；详情页用来定位当前文档所在分类）
    cat_nodes = {c.pk: {'cat': c, 'children': [], 'docs': docs_by_cat.get(c.pk, [])}
                 for c in categories}
    cat_roots = []
    for c in categories:
        if c.parent_id and c.parent_id in cat_nodes:
            cat_nodes[c.parent_id]['children'].append(cat_nodes[c.pk])
        else:
            cat_roots.append(cat_nodes[c.pk])
    # 选中分类的祖先链默认展开，其余子树折叠（避免右栏一屏放不下）
    open_ids = set()
    sel_id = _int_or_none(selected_category_id)
    sel = cat_nodes.get(sel_id) if sel_id is not None else None
    while sel:
        open_ids.add(sel['cat'].pk)
        sel = cat_nodes.get(sel['cat'].parent_id)

    # 统计数据（聚合下推到数据库，避免把整表对象加载进 Python 求和）
    visible = access.visible_docs(user).filter(status='published')
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

    return {
        'categories': categories,
        'category_tree': {'roots': cat_roots},
        'open_ids': open_ids,
        'selected_category': str(sel_id) if sel_id is not None else '',
        'selected_tag': selected_tag or '',
        'current_doc_id': current_doc_id,
        'total_documents': total_documents,
        'total_categories': total_categories,
        'total_views': total_views,
        'avg_views': avg_views,
        'tag_list': tag_list,
    }


@perm_required("knowledge", "read")
def document_list(request):
    """文档列表（读路径改造：所有统计/标签/分类计数只算当前用户可见范围）"""
    category_id = request.GET.get('category')
    tag_filter = request.GET.get('tag')
    # 唯一入口 access.visible_docs：公开 + 本人文档；漏一处就是泄漏口
    documents = access.visible_docs(request.user).filter(status='published')

    # —— 文件夹过滤（批次 6）：侧栏是「我的文档」树，过滤只作用于本人文档；
    #    别人的文件夹 id 一律忽略（不报错不泄露），回落到不过滤的全量视图
    folder_id = request.GET.get('folder')
    current_folder = None
    if folder_id == '0':   # 约定 0 = 未归档
        documents = documents.filter(owner=request.user, folder__isnull=True)
        current_folder = 'unarchived'
    elif folder_id:
        folder = kb_folders._owned_folder(request.user, folder_id)
        if folder is not None:
            documents = documents.filter(folder=folder)
            current_folder = folder

    if category_id:
        documents = documents.filter(category_id=category_id)

    if tag_filter:
        documents = documents.filter(tags__icontains=tag_filter)

    # 后端分页（沿用 records.crud._per_page：10/30/50）
    per_page = _per_page(request)
    page_obj = Paginator(documents, per_page).get_page(request.GET.get('page'))

    context = {
        'documents': page_obj,
        'page_obj': page_obj,
        'per_page': per_page,
        'current_folder': current_folder,
        **_sidebar_context(request.user, category_id, tag_filter),
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
    # 允许标题 id：用于 Markdown 自动生成的目录锚点 (#heading)
    "h1": ["id"], "h2": ["id"], "h3": ["id"],
    "h4": ["id"], "h5": ["id"], "h6": ["id"],
}


# 裸 rel="noopener noreferrer" 常量（目标 _blank 统一补齐）
_NOOPENER_RE = re.compile(r'(<a\b[^>]*?\btarget=["\']_blank["\'][^>]*?)\srel=["\'][^"\']*["\']', re.IGNORECASE)
_NO_REL_PATTERN = re.compile(r'(<a\b[^>]*?\btarget=["\']_blank["\'])(?![^>]*\brel=)', re.IGNORECASE)


def _sanitize_html(html: str) -> str:
    import bleach

    cleaned = bleach.clean(
        html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS,
        protocols=["http", "https", "mailto"], strip=True,
    )
    # L2：target=_blank 必须带 rel="noopener noreferrer"，否则新窗口能通过 window.opener 反向控制父页面
    cleaned = _NOOPENER_RE.sub(r'\1 rel="noopener noreferrer"', cleaned)      # 已有 rel 属性 → 覆盖
    cleaned = _NO_REL_PATTERN.sub(r'\1 rel="noopener noreferrer"', cleaned)  # 没有 rel → 追加
    return cleaned


def _vis_form_context(document=None, user=None):
    """表单/详情模板共用的可见性上下文（单选框 + 徽章 + 切换按钮全用它）。"""
    return {
        'visibility_choices': access.visibility_choices_for_ui(),
        'current_visibility': document.visibility if document is not None else access.PRIVATE,
        'can_manage_vis': True if document is None else access.can_manage_visibility(document, user),
        'visibility_label': access.visibility_label(document),
    }


# ---------------------------------------------------------------- 详情页「书房化」阅读体验

# GFM 任务列表行首标记：python-markdown 不识别，输出为纯文字，转 HTML 后替换为 checkbox
_TASK_LINE_RE = re.compile(r'<li>\[( |x|X)\]\s*')


def _render_markdown(text: str) -> str:
    """Markdown → 安全 HTML（含 GFM 任务列表 checkbox 化）。

    python-markdown 没有内置 tasklist 扩展，``- [ ] `` 会原样印成文字。
    在 bleach 净化**之后**替换行首标记为禁用态 checkbox：语义可读
    （屏幕阅读器报 checkbox），又不动白名单、不引新依赖。
    """
    html = _sanitize_html(markdown.markdown(
        text,
        extensions=[
            'markdown.extensions.extra',
            'markdown.extensions.codehilite',
            'markdown.extensions.toc',
        ]
    ))
    return _TASK_LINE_RE.sub(
        lambda m: '<li class="kb-task-item">'
                  '<input type="checkbox" disabled%s> '
                  % (' checked' if m.group(1).lower() == 'x' else ''),
        html,
    )


def _category_path(cat):
    """当前文档的分类路径（根 → 叶子），供面包屑使用；防 parent 环死循环。"""
    path, seen = [], set()
    while cat and cat.pk not in seen:
        seen.add(cat.pk)
        path.append(cat)
        cat = cat.parent
    return list(reversed(path))


@perm_required("knowledge", "read")
def document_detail(request, pk):
    """文档详情（无权访问一律 404：403 等于告诉对方「这篇存在，只是你没权限」）

    详情页已取消 AJAX 弹窗（用户要求），一律整页渲染；浏览计数随之不再
    区分请求头——带 X-Requested-With 的请求也按正常浏览计入。
    """
    document = get_object_or_404(access.visible_docs(request.user), pk=pk)
    # 原子自增（F 表达式），避免并发浏览时的读-改-写覆盖导致计数丢失
    KnowledgeDocument.objects.filter(pk=document.pk).update(
        view_count=F('view_count') + 1)
    document.refresh_from_db(fields=['view_count'])
    # 阅读明细表：每次打开详情页写一行
    KnowledgeView.objects.create(
        document=document,
        user=request.user if request.user.is_authenticated else None,
        viewer=request.user.get_username() if request.user.is_authenticated else '',
        ip=client_ip(request),
    )

    # 将Markdown内容转换为HTML，并过滤 script/iframe/事件属性等危险内容
    # （_render_markdown：净化 + 任务列表 checkbox 化）
    document.content_html = _render_markdown(document.content)

    # 获取相关文档（同分类或同标签；同样只从可见范围里挑，
    # 否则会通过「相关文档」把私有文档的标题带出来）
    # L4：document.tags 为空时跳过标签分支——icontains='' 会匹配全表，导致相关文档杂乱
    related_q = Q(category=document.category)
    first_tag = (document.tags or "").split(",")[0].strip()
    if first_tag:
        related_q |= Q(tags__icontains=first_tag)
    related_docs = access.visible_docs(request.user).filter(related_q).exclude(pk=pk).distinct()[:5]

    context = {
        'document': document,
        'related_docs': related_docs,
        'category_path': _category_path(document.category),
        'can_edit_doc': access.can_edit(document, request.user),
        # 个人书架路径（批次 6）：仅 owner 自己的文件夹链；他人的文件夹一律 []
        'folder_breadcrumbs': kb_folders.breadcrumbs(document.folder_id, request.user),
        # 右栏与列表页同款（_right_sidebar.html）：统计/分类筛选树/标签云；
        # 选中项=当前文档所在分类，树会自动展开其祖先链并高亮；
        # current_doc_id 让树里的当前文档行也高亮
        **_sidebar_context(request.user, document.category_id,
                           current_doc_id=document.pk),
        **_vis_form_context(document, request.user),
    }
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
            try:
                log_operation(
                    request, '知识库', '文档创建',
                    f'知识文档#{doc.pk}',
                    f'「{doc.title}」创建',
                    after=snapshot(doc),
                )
            except Exception:
                import logging
                logging.getLogger(__name__).exception('知识库文档#%s 创建留痕失败', doc.pk)
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
            before = snapshot(document)
            form.save()
            # 可见性单选框（批次 2 上 UI）：切换走唯一入口并留痕
            target = request.POST.get('visibility')
            if target in access.VISIBILITY_LABELS and access.can_manage_visibility(document, request.user):
                access.set_visibility(document, target, request=request, source='编辑表单')
            try:
                document.refresh_from_db()
                log_operation(
                    request, '知识库', '文档编辑',
                    f'知识文档#{document.pk}',
                    f'「{document.title}」编辑',
                    before=before, after=snapshot(document),
                )
            except Exception:
                import logging
                logging.getLogger(__name__).exception('知识库文档#%s 编辑留痕失败', document.pk)
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


def _doc_tree(document):
    """文档 → 树 dict（接口/前端统一形状）。

    editor_mode=outline 的文档从节点表组装；markdown 文档即时解析 content，
    不落库（P1 只读阶段零数据变更；首次在编辑器保存后才转 outline，见 4b）。
    """
    if document.editor_mode == 'outline':
        nodes = document.nodes.all().order_by('sort', 'id')
        by_parent = {}
        for n in nodes:
            by_parent.setdefault(n.parent_id, []).append(n)

        def build(parent_id):
            return [{
                'uid': n.uid,
                'type': n.node_type,
                'text': n.text,
                'done': n.is_done,
                'note': n.note,
                'due': n.due_date.isoformat() if n.due_date else None,
                'heading_level': n.heading_level,
                'collapsed': n.collapsed,
                'ref_module': n.ref_module,
                'ref_object_id': n.ref_object_id,
                'ref_label': n.ref_label,
                'children': build(n.pk),
            } for n in by_parent.get(parent_id, [])]

        return build(None)
    return kb_outline.md_to_tree(document.content)


@perm_required("knowledge", "read")
def api_doc_tree(request, pk):
    """大纲数据源：GET 只读（P1）/ PUT 整树保存（4b），同路径按方法分发。

    无权访问一律 404，不泄露文档存在性；PUT 额外要求 knowledge.update + owner。
    """
    document = get_object_or_404(access.visible_docs(request.user), pk=pk)
    if request.method == 'PUT':
        return _api_save_tree(request, document)
    return JsonResponse({
        'ok': True,
        'version': document.tree_version,
        'editor_mode': document.editor_mode,
        'tree': _doc_tree(document),
    })


# —— 整树保存（方案 03 第五节伪代码的落地版）——
MAX_NODES = 1000   # D3 拍板：节点上限 1000（方案原文 5000 收紧）
MAX_DEPTH = 20
_NODE_TYPES = {'text', 'todo', 'code', 'quote', 'divider', 'ref'}


def _parse_due(value):
    """ISO 日期字符串 → date；非法/缺失返回 None，绝不抛异常。"""
    try:
        return date.fromisoformat(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _normalize_tree(raw_tree):
    """客户端树 → 可入库树：补 uid、白名单类型、待办日期前缀转写。

    返回 (tree, node_count)。转写规则与前端编辑器一致（outline.split_todo_due）：
    待办文本以 ``YYYY-MM-DD`` 开头 → 提取为 due 并剥离前缀；否则用节点携带的
    due 字段（编辑器回传既有值）；都没有 → 清空。非法日期一律视为无到期日。
    """
    def walk(nodes, out):
        for raw in nodes or []:
            if not isinstance(raw, dict):
                continue
            ntype = raw.get('type') if raw.get('type') in _NODE_TYPES else 'text'
            text = str(raw.get('text') or '')[:10000]
            due = None
            if ntype == 'todo':
                due_str, rest = kb_outline.split_todo_due(text)
                if due_str:
                    due = _parse_due(due_str)
                    if due:
                        text = rest
                else:
                    due = _parse_due(raw.get('due'))
            node = {
                'uid': str(raw.get('uid') or uuid.uuid4().hex[:12])[:12],
                'type': ntype,
                'text': text,
                'done': bool(raw.get('done')),
                'note': str(raw.get('note') or '')[:50000],
                'due': due,
                'heading_level': (min(max(int(raw.get('heading_level') or 1), 1), 3)
                                  if ntype == 'heading' else None),
                'collapsed': bool(raw.get('collapsed')),
                'ref_module': str(raw.get('ref_module') or '')[:32],
                'ref_object_id': (int(raw['ref_object_id'])
                                  if str(raw.get('ref_object_id') or '').isdigit() else None),
                'ref_label': str(raw.get('ref_label') or '')[:200],
                'children': [],
            }
            out.append(node)
            walk(raw.get('children'), node['children'])
        return out

    tree = walk(raw_tree, [])
    return tree, kb_outline.count_nodes(tree)


def _api_save_tree(request, document):
    """PUT 整树保存：乐观锁 409 + 层序落库 + content 兼容层同步写回。

    - 权限：模块 update（外层已过 read）+ access.can_edit（owner 专属，
      公开文档他人只读）；全程留痕，留痕失败只记日志不阻断保存。
    - 并发：select_for_update 行锁内校验 base_version，过期 → 409 带最新版本。
    - uid 缺失时服务端补 12 位；未出现的 uid 整节点级联删除。
    """
    if not has_perm(request.user, 'knowledge', 'update'):
        return JsonResponse({'ok': False, 'message': '权限不足，无法保存大纲。'}, status=403)
    if not access.can_edit(document, request.user):
        return JsonResponse({'ok': False, 'message': '只能编辑自己创建的文档'}, status=403)

    try:
        payload = json.loads(request.body or b'{}')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'ok': False, 'message': '请求体不是合法 JSON'}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({'ok': False, 'message': '请求体格式错误'}, status=400)

    tree, node_count = _normalize_tree(payload.get('tree') or [])
    if node_count > MAX_NODES or kb_outline.depth_of(tree) > MAX_DEPTH:
        return JsonResponse({
            'ok': False,
            'message': f'文档过大或层级过深（上限 {MAX_NODES} 个节点 / {MAX_DEPTH} 层）',
        }, status=400)

    try:
        with transaction.atomic():
            locked = KnowledgeDocument.objects.select_for_update().get(pk=document.pk)
            if payload.get('base_version') != locked.tree_version:
                return JsonResponse({
                    'ok': False, 'error': 'conflict', 'version': locked.tree_version,
                    'message': '该文档在别处被修改，保存被拒绝',
                }, status=409)

            existing = {n.uid: n for n in locked.nodes.all()}
            seen, to_create, to_update = set(), [], []

            def walk(nodes, parent):
                for i, node in enumerate(nodes):
                    uid = node['uid']
                    if uid in seen:
                        # 前端 bug 兜底：同一 uid 出现两次，视为新节点避免互相覆盖
                        node = dict(node, uid=uuid.uuid4().hex[:12])
                        uid = node['uid']
                    seen.add(uid)
                    row = existing.get(uid)
                    if row is None:
                        row = KnowledgeNode(document=locked, uid=uid)
                        to_create.append(row)
                    else:
                        to_update.append(row)
                    row.parent = parent
                    row.sort = i
                    row.text = node['text']
                    row.note = node['note']
                    row.node_type = node['type']
                    row.is_done = node['done']
                    row.due_date = node['due']
                    row.collapsed = node['collapsed']
                    row.heading_level = node['heading_level']
                    row.ref_module = node['ref_module']
                    row.ref_object_id = node['ref_object_id']
                    row.ref_label = node['ref_label']
                    walk(node['children'], row)

            walk(tree, None)
            # 新节点逐个 create（bulk_create 拿不到 pk，子节点挂不上 parent）；
            # DFS 前序保证父行先入库。1000 节点单事务在 SQLite 上秒级完成。
            for row in to_create:
                row.save()
            if to_update:
                KnowledgeNode.objects.bulk_update(to_update, [
                    'parent', 'sort', 'text', 'note', 'node_type', 'is_done',
                    'due_date', 'collapsed', 'heading_level',
                    'ref_module', 'ref_object_id', 'ref_label',
                ], batch_size=200)
            if seen:
                locked.nodes.exclude(uid__in=seen).delete()
            else:
                locked.nodes.all().delete()

            old_version = locked.tree_version
            locked.tree_version += 1
            locked.editor_mode = 'outline'
            locked.content = kb_outline.tree_to_md(tree)   # 兼容层同步写回
            locked.save(update_fields=['tree_version', 'editor_mode', 'content', 'updated_at'])
    except Exception:
        import logging
        logging.getLogger(__name__).exception('知识库文档 #%s 整树保存失败', document.pk)
        return JsonResponse({'ok': False, 'message': '保存失败，请稍后重试'}, status=500)

    try:
        log_operation(
            request, '知识库', '大纲保存', f'知识文档#{locked.pk}',
            f'「{locked.title}」整树保存（{node_count} 个节点，'
            f'v{old_version} → v{locked.tree_version}）',
        )
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            '知识库文档 #%s 大纲保存留痕失败', locked.pk)

    return JsonResponse({'ok': True, 'version': locked.tree_version})


@perm_required("knowledge", "update")
def outline_editor(request, pk):
    """大纲编辑器页（4b）：owner 专属；他人公开文档跳详情只读，私有 404。"""
    document = get_object_or_404(access.visible_docs(request.user), pk=pk)
    if not access.can_edit(document, request.user):
        return redirect('knowledge_base:document_detail', pk=pk)
    context = {
        'document': document,
        'tree_version': document.tree_version,
        'tree_data': {'tree': _doc_tree(document), 'version': document.tree_version},
        **_vis_form_context(document, request.user),
    }
    return render(request, 'knowledge_base/outline_editor.html', context)


# —— 文件夹树接口（批次 6 / 方案 04）：读 GET、变更一律 POST（删除禁止 GET）——

def _folder_payload(request):
    try:
        data = json.loads(request.body or b'{}')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


@perm_required("knowledge", "read")
def api_folders(request):
    """GET 返回整棵树 + 未归档；POST 新建文件夹（400 带 FolderError 文案）。"""
    if request.method == 'POST':
        data = _folder_payload(request)
        try:
            folder = kb_folders.create_folder(
                request.user, data.get('name'), data.get('parent_id'))
        except kb_folders.FolderError as e:
            return JsonResponse({'ok': False, 'message': str(e)}, status=400)
        try:
            log_operation(
                request, '知识库', '文件夹创建',
                f'文件夹#{folder.pk}',
                f'「{folder.name}」创建',
            )
        except Exception:
            import logging
            logging.getLogger(__name__).exception('知识库文件夹#%s 创建留痕失败', folder.pk)
        return JsonResponse({'ok': True, 'id': folder.pk, 'name': folder.name})
    tree = kb_folders.folder_tree(request.user)
    return JsonResponse({'ok': True, **tree})


@perm_required("knowledge", "update")
def api_folder_rename(request, pk):
    data = _folder_payload(request)
    try:
        folder = kb_folders.rename_folder(request.user, pk, data.get('name'))
    except kb_folders.FolderError as e:
        return JsonResponse({'ok': False, 'message': str(e)}, status=400)
    try:
        log_operation(
            request, '知识库', '文件夹重命名',
            f'文件夹#{folder.pk}',
            f'「{folder.name}」重命名',
        )
    except Exception:
        import logging
        logging.getLogger(__name__).exception('知识库文件夹#%s 重命名留痕失败', folder.pk)
    return JsonResponse({'ok': True, 'id': folder.pk, 'name': folder.name})


@perm_required("knowledge", "update")
def api_folder_move(request, pk):
    data = _folder_payload(request)
    try:
        folder = kb_folders.move_folder(request.user, pk, data.get('parent_id'))
    except kb_folders.FolderError as e:
        return JsonResponse({'ok': False, 'message': str(e)}, status=400)
    try:
        log_operation(
            request, '知识库', '文件夹移动',
            f'文件夹#{folder.pk}',
            f'「{folder.name}」{"移至顶层" if folder.parent_id is None else "移动到子文件夹"}',
        )
    except Exception:
        import logging
        logging.getLogger(__name__).exception('知识库文件夹#%s 移动留痕失败', folder.pk)
    return JsonResponse({'ok': True, 'id': folder.pk, 'parent_id': folder.parent_id})


@perm_required("knowledge", "update")
def api_folder_delete(request, pk):
    """只删壳：子文件夹与文档上提一层，返回受影响文档数。"""
    try:
        moved_docs = kb_folders.delete_folder(request.user, pk)
    except kb_folders.FolderError as e:
        return JsonResponse({'ok': False, 'message': str(e)}, status=400)
    try:
        log_operation(
            request, '知识库', '文件夹删除',
            f'文件夹#{pk}',
            f'删除（{moved_docs} 篇文档上提）',
        )
    except Exception:
        import logging
        logging.getLogger(__name__).exception('知识库文件夹#%s 删除留痕失败', pk)
    return JsonResponse({'ok': True, 'moved_docs': moved_docs})


@perm_required("knowledge", "update")
def api_doc_move(request, pk):
    """文档归档/移出：can_edit（owner 专属）失败 403，文件夹非法 400。"""
    document = get_object_or_404(access.visible_docs(request.user), pk=pk)
    if not access.can_edit(document, request.user):
        return JsonResponse({'ok': False, 'message': '只能移动自己创建的文档'}, status=403)
    data = _folder_payload(request)
    target_folder = kb_folders._owned_folder(request.user, data.get('folder_id'))
    target_label = ('未归档' if target_folder is None and data.get('folder_id') not in (None, 0, '0', '')
                    else (target_folder.name if target_folder else ''))
    try:
        kb_folders.move_doc(request.user, document, data.get('folder_id'))
    except kb_folders.FolderError as e:
        return JsonResponse({'ok': False, 'message': str(e)}, status=400)
    try:
        log_operation(
            request, '知识库', '文档归档',
            f'知识文档#{pk}',
            f'「{document.title}」移动到「{target_label or "未归档"}」',
        )
    except Exception:
        import logging
        logging.getLogger(__name__).exception('知识库文档#%s 归档留痕失败', pk)
    return JsonResponse({'ok': True})


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
        before = snapshot(document)
        title = document.title
        doc_id = document.pk
        document.delete()
        try:
            log_operation(
                request, '知识库', '文档删除',
                f'知识文档#{doc_id}',
                f'「{title}」删除',
                before=before,
            )
        except Exception:
            import logging
            logging.getLogger(__name__).exception('知识库文档#%s 删除留痕失败', doc_id)
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
            try:
                log_operation(
                    request, '知识库', '分类创建',
                    f'分类#{cat.pk}',
                    f'「{cat.name}」创建',
                    after=snapshot(cat),
                )
            except Exception:
                import logging
                logging.getLogger(__name__).exception('知识库分类#%s 创建留痕失败', cat.pk)
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
            before = snapshot(category)
            form.save()
            try:
                category.refresh_from_db()
                log_operation(
                    request, '知识库', '分类编辑',
                    f'分类#{category.pk}',
                    f'「{category.name}」编辑',
                    before=before, after=snapshot(category),
                )
            except Exception:
                import logging
                logging.getLogger(__name__).exception('知识库分类#%s 编辑留痕失败', category.pk)
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
        before = snapshot(category)
        cat_id = category.pk
        cat_name = category.name
        document_count = category.documents.count()
        category.delete()
        try:
            log_operation(
                request, '知识库', '分类删除',
                f'分类#{cat_id}',
                f'「{cat_name}」删除（含 {document_count} 篇文档）',
                before=before,
            )
        except Exception:
            import logging
            logging.getLogger(__name__).exception('知识库分类#%s 删除留痕失败', cat_id)
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
