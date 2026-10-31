"""检索类工具：跨模块关键词搜索 + 知识库检索。"""

import re

from django.db.models import Q

from .. import catalog, registry, security
from .common import base_queryset, labels, project, resolve_order

MAX_PER_MODULE = 8
MAX_TOTAL = 30
MAX_TERMS = 4

_SPLIT_RE = re.compile(r'[\s,，、/;；|]+')


def split_terms(keyword):
    """把「华建建材 客诉」拆成可分别匹配的词。

    整串 icontains 几乎不可能命中：没有任何字段同时等于"华建建材 客诉"。
    拆开后按"每个词都能在某个关键词字段里出现"来匹配，才符合人的直觉。
    """
    raw = _SPLIT_RE.split((keyword or '').strip())
    terms = [t for t in raw if len(t) >= 2]
    if not terms:
        stripped = (keyword or '').strip()
        return [stripped] if stripped else []
    return terms[:MAX_TERMS]


def _match_all(spec, user, terms):
    """所有词都命中（每个词在自己的 OR 条件内）才算一条记录。"""
    qs = base_queryset(spec, user)
    cond = None
    for term in terms:
        sub = Q()
        for field in spec.keyword_fields:
            sub |= Q(**{f'{field}__icontains': term})
        cond = sub if cond is None else (cond & sub)
    return qs.filter(cond) if cond is not None else qs.none()


@registry.tool(
    'global_search',
    '按关键词在多个模块里跨表搜索，返回各模块命中的记录。适合"XX 客户相关的所有记录"这类问题。'
    '关键词可用空格分隔多个词（如"华建建材 客诉"），会按"每个词都命中"匹配。',
    params=(
        registry.Param('keyword', 'string', required=True, maximum=60,
                       description='关键词，客户名/案号/项目名等，多个词用空格分隔'),
        registry.Param('limit', 'int', default=MAX_PER_MODULE, minimum=1,
                       maximum=MAX_PER_MODULE, description='每个模块最多几条'),
    ),
    examples=('搜一下华建相关的记录', '有没有提到延期的客诉'),
)
def global_search(ctx, keyword, limit=MAX_PER_MODULE):
    pol = ctx.policy or security.policy()
    modules = registry.available_modules(ctx.user, ctx.is_local, pol)
    if not modules:
        return registry.ToolResult(ok=False, error='NO_MODULE',
                                   message='当前账号没有可查询的模块')

    terms = split_terms(keyword)
    results, refs, total = [], [], 0
    for spec in modules:
        if not spec.keyword_fields or not terms:
            continue
        try:
            qs = _match_all(spec, ctx.user, terms)
            count = qs.count()
            # 「华建建材 客诉」这类组合词在部分模块没有"客诉"字段（客户名在、
            # 描述里未必有），严格匹配会全军覆没；退一步只按首个词搜，
            # 至少把这家客户的记录找出来。
            if not count and len(terms) > 1:
                qs = _match_all(spec, ctx.user, terms[:1])
                count = qs.count()
        except Exception:  # noqa: BLE001 - 单模块失败不影响其它模块
            continue
        if not count:
            continue
        rows = list(qs[:limit])
        projected = []
        for obj in rows:
            row, _ = project(spec, obj, spec.list_fields, pol)
            row['id'] = obj.pk
            projected.append(row)
            refs.append(catalog.build_ref(spec, obj))
        results.append({
            'module': spec.key,
            'module_label': spec.label,
            'total': count,
            'columns': labels(spec, spec.list_fields),
            'rows': projected,
        })
        total += count
        if len(refs) >= MAX_TOTAL:
            break

    if not results:
        # 0 条不等于"系统里没有"：模型常把模块名（"试样""审厂"）当关键词来搜，
        # 而模块名不是任何字段的值，必然搜不到。
        from ..engine import planner      # 延迟导入，避免 tools ⇄ engine 循环
        named = planner.hinted_modules(keyword, modules)
        for spec in named[:2]:
            try:
                qs = base_queryset(spec, ctx.user).order_by(resolve_order(spec, ''))
                count = qs.count()
            except Exception:  # noqa: BLE001
                continue
            if not count:
                continue
            rows = list(qs[:limit])
            projected = []
            for obj in rows:
                row, _ = project(spec, obj, spec.list_fields, pol)
                row['id'] = obj.pk
                projected.append(row)
                refs.append(catalog.build_ref(spec, obj))
            results.append({
                'module': spec.key,
                'module_label': spec.label,
                'total': count,
                'columns': labels(spec, spec.list_fields),
                'rows': projected,
                'matched_by': 'module_name',   # 提示模型：这是"按模块名列出"，不是关键词命中
            })
            total += count
            if len(refs) >= MAX_TOTAL:
                break
        if results:
            return registry.ToolResult(
                ok=True,
                data={
                    'keyword': keyword,
                    'terms': split_terms(keyword),
                    'groups': results,
                    'total': total,
                    'hint': (f'「{keyword}」是模块名而非字段值，已改为列出该模块最近的记录。'
                             '要筛选（如"没通过""待测试"）请改用 query_records，'
                             'keyword 传选项名或记录编号。'),
                    'choices': catalog.choice_options(results[0]['module']) if results else {},
                },
                row_count=len(refs), refs=refs[:MAX_TOTAL],
                truncated=total > len(refs),
            )
        if named:
            keys = ', '.join(m.key for m in named[:3])
            hint = (f'「{keyword}」是模块名，不是记录字段的值，所以搜不到。'
                    f'要列该模块的记录，请调用 query_records，module={keys}。')
        else:
            hint = ('没有匹配记录。可改用 query_records 查具体模块，'
                    '或请用户补充客户名/单号/日期区间。')
        return registry.ToolResult(
            ok=True, data={'keyword': keyword, 'groups': [], 'total': 0, 'hint': hint},
            row_count=0, message=hint,
        )
    return registry.ToolResult(
        ok=True,
        data={'keyword': keyword, 'terms': terms, 'groups': results, 'total': total},
        row_count=len(refs), refs=refs[:MAX_TOTAL],
        truncated=total > len(refs),
    )


@registry.tool(
    'search_knowledge',
    '在知识库中检索已发布文档，返回文档标题与内容片段。',
    params=(
        registry.Param('keyword', 'string', required=True, maximum=60),
        registry.Param('limit', 'int', default=5, minimum=1, maximum=10),
    ),
    module_key='knowledge',
    examples=('知识库里有没有客诉处理流程',),
)
def search_knowledge(ctx, keyword, limit=5):
    from django.urls import reverse
    from knowledge_base import access as kb_access
    from knowledge_base.models import KnowledgeDocument

    qs = kb_access.visible_docs(
        ctx.user, KnowledgeDocument.objects.select_related('category')
    ).filter(status='published').filter(
        Q(title__icontains=keyword) | Q(content__icontains=keyword)
        | Q(summary__icontains=keyword) | Q(tags__icontains=keyword)
    )[:limit]

    rows, refs = [], []
    for doc in qs:
        summary = (doc.summary or doc.content or '')[:200]
        rows.append({
            'id': doc.pk,
            'title': doc.title,
            'summary': security.mask_text(summary),
            'category': getattr(doc.category, 'name', '') if doc.category_id else '',
        })
        try:
            url = reverse('knowledge_base:document_detail', args=[doc.pk])
        except Exception:  # noqa: BLE001
            url = catalog.safe_reverse('document_detail', [doc.pk])
        refs.append({
            'module': 'knowledge', 'module_label': '知识库', 'id': doc.pk,
            'label': doc.title[:60], 'url': url,
        })
    return registry.ToolResult(
        ok=True, data={'keyword': keyword, 'rows': rows}, row_count=len(rows),
        refs=refs,
    )
