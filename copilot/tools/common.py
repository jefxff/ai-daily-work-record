"""工具共用的行投影与查询构造。"""

from datetime import date, datetime
from decimal import Decimal

from django.db.models import Model, Q

from .. import catalog, security


def base_queryset(spec, user):
    """模块基础查询集；``scope='own'`` 的模块强制只查本人数据。

    ``scope='visible'`` 用于知识库：可见性规则由 knowledge_base.access 统一
    收口（全员可见 + 本人私有 + 有权限者可读他人私有），工具层不另写一套。
    """
    qs = catalog.model_of(spec).objects.all()
    if spec.scope == 'own':
        qs = qs.filter(user=user)
    elif spec.scope == 'visible':
        from knowledge_base.access import visible_docs   # 延迟导入，避免 app 加载顺序问题
        qs = visible_docs(user, qs)
    if spec.base_filter:
        # 模块级固定条件（知识库"仅已发布"、将来可能的"仅有效"之类）
        qs = qs.filter(spec.base_filter(user))
    # 软删的记录对 AI 也不存在：否则会在"有没有找到"和列表页之间对不上
    if spec.soft_delete_field:
        qs = qs.filter(**{spec.soft_delete_field: False})
    return qs


_NEG_WORDS = '没未不非'


def _choice_conditions(spec, keyword):
    """把选项标签翻成库里存的代码："不通过" → ``audit_result='fail'``。

    样例模块的状态是英文代码（pass/fail/conditional、pending/testing…），
    但用户只会说"没通过""待测试"。不做这层翻译，关键词检索永远 0 条，
    模型只能答"没有记录"。

    两个细节：
    1. 否定词是口语的常态——"没通过"里的"通过"要落到"不通过"(fail)，
       而不是"通过"(pass)，否则答案正好反了；
    2. 只保留最长的匹配标签：问"不合格"时同时命中"合格"和"不合格"，
       取长的才对。
    """
    try:
        model = catalog.model_of(spec)
    except Exception:  # noqa: BLE001
        return Q()

    def negated_at(text, label):
        idx = text.find(label)
        return idx > 0 and text[idx - 1] in _NEG_WORDS

    matched = []
    for field in model._meta.concrete_fields:
        choices = [(value, str(label)) for value, label in
                   (getattr(field, 'choices', None) or ()) if label]
        for value, label in choices:
            if label not in keyword:
                continue
            if negated_at(keyword, label):
                # "没通过" → 同字段里带否定词的同义选项（"不通过"）
                alt = [c for c in choices
                       if c[1] != label and label in c[1]
                       and any(word in c[1] for word in _NEG_WORDS)]
                if alt:
                    matched.append((field.name, alt[0][0], alt[0][1]))
                    continue
            matched.append((field.name, value, label))

    keep = [m for m in matched
            if not any(m[2] != o[2] and m[2] in o[2] for o in matched)]
    cond = Q()
    for name, value, _label in keep:
        cond |= Q(**{name: value})
    return cond



def apply_filters(qs, spec, keyword='', date_from='', date_to='', state=None,
                  keyword_field=''):
    """关键词 / 日期区间 / 业务状态三层筛选。

    ``state`` 是 ``catalog.StateSpec``（"已过期""借出中"…）：这类状态在库里
    不是一个字段，必须由模块自己给出可执行的 Q，否则只能靠模型瞎猜。

    ``keyword_field`` 是**已经确认归属**的字段（值就在该字段里出现过）。
    这时用精确匹配而不是 ``icontains``：模糊匹配会让"元/吨"同时命中"美元/吨"，
    统计出来的数就比真实值多出一截。模型自己传 keyword 时不给这个字段，
    仍走模糊匹配（它可能只给了半个词）。
    """
    fields = catalog.search_fields(spec)
    target = keyword_field if keyword_field in fields else ''
    if keyword and fields:
        cond = Q()
        if target:
            cond |= Q(**{f'{target}__iexact': keyword})
        cond |= _choice_conditions(spec, keyword)
        if not target:
            for field in fields:
                cond |= Q(**{f'{field}__icontains': keyword})
        qs = qs.filter(cond)
    if spec.date_field:
        if date_from:
            qs = qs.filter(**{f'{spec.date_field}__gte': date_from})
        if date_to:
            qs = qs.filter(**{f'{spec.date_field}__lte': date_to})
    if state is not None:
        # 状态可能是 Q（行级条件），也可能是归组钩子（保险"按标的最新年度"），
        # 一律交给 StateSpec 自己决定，工具层不需要知道差异。
        qs = state.apply_to(qs)
    return qs


def resolve_order(spec, order_by):
    """排序白名单校验；返回 None 表示非法字段。"""
    if not order_by:
        return f'-{spec.date_field}' if spec.date_field else '-pk'
    desc = order_by.startswith('-')
    name = order_by.lstrip('-')
    allowed = set(spec.order_fields) | set(spec.list_fields) | {'pk', 'id'}
    if spec.date_field:
        allowed.add(spec.date_field)
    if name not in allowed:
        return None
    return f'-{name}' if desc else name


def project(spec, obj, fields, pol, text_limit=120):
    """把模型对象投影成 dict：格式化 → 脱敏 → 截断。

    截断只针对字符串（长文本字段如客诉描述），数值/日期原样保留，
    避免模型看到半截数字胡编。
    """
    row = {}
    for name in fields:
        value = getattr(obj, name, None)
        if isinstance(value, datetime):
            value = value.strftime('%Y-%m-%d %H:%M')
        elif isinstance(value, date):
            value = value.isoformat()
        elif isinstance(value, Decimal):
            value = str(value)
        elif isinstance(value, Model):
            # 外键（档案所属柜/台账/类别、知识文档分类）：给名称而不是对象 repr，
            # 否则 JSON 序列化会变成 "Cabinet object (3)" 甚至直接报错。
            value = str(value)
        row[name] = value

    row, masked = security.mask_row(spec, row, getattr(pol, 'mask_enabled', True))
    for name, value in list(row.items()):
        if isinstance(value, str) and len(value) > text_limit:
            row[name] = value[:text_limit] + '…'
    return row, masked


def labels(spec, fields):
    return {f: catalog.field_label(spec, f) for f in fields}


def jsonable(value):
    """聚合结果转成可 JSON 序列化的值。"""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value
