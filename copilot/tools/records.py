"""记录类工具：列表查询、单条详情、聚合统计。"""

from urllib.parse import urlencode

from django.db.models import Avg, Count, Max, Min, Sum
from django.urls import NoReverseMatch, reverse

from .. import catalog, registry
from .common import apply_filters, base_queryset, jsonable, labels, project, resolve_order

MAX_ROWS = 50
MAX_REFS = 30
AGG_FUNCS = {'count': Count, 'sum': Sum, 'avg': Avg, 'max': Max, 'min': Min}


def _empty_hint(spec, ctx, date_from='', date_to='', state_label=''):
    """0 条时，区分"真的没有"和"日期猜错了"。

    小模型常把"最近"理解成 2023/2024 年，给了区间却查不到，然后直接回一句
    "没有记录"——这比查不到更糟。把该模块实际最新一条的日期告诉它，
    它才有可能改区间重试。
    """
    if state_label:
        return f'「{spec.label}」中状态为「{state_label}」的记录是 0 条。'
    if not (date_from or date_to) or not spec.date_field:
        return '该模块在这组条件下没有记录。'
    try:
        latest = (base_queryset(spec, ctx.user)
                  .order_by(f'-{spec.date_field}')
                  .values_list(spec.date_field, flat=True).first())
    except Exception:  # noqa: BLE001
        latest = None
    if latest:
        return (f'所查日期区间内没有记录。该模块最新一条的日期是 {latest}，'
                '请据此调整 date_from/date_to 后重试。')
    return '该模块目前没有任何记录。'


def _state_of(spec, state):
    """参数 state → StateSpec；不支持时给出**可执行的下一步**，不是一句"不支持"。

    反例来自线上：模型把"即将到期"当 state 传给保险模块，工具回"该模块不支持"，
    模型看不懂就原样重试两次，最后答"没有记录"。所以拒绝信息里必须写清
    "这个模块能筛什么 / 想按时间筛该用哪个参数"，它才知道怎么改。
    """
    if not state:
        return None, None
    spec_state = catalog.get_state(spec, state)
    if spec_state is None:
        options = catalog.state_options(spec)
        if options:
            hint = f'该模块不支持 state={state}；可用状态：{"、".join(options)}。'
        else:
            reason = spec.no_state_reason or '该模块没有业务状态可选'
            hint = f'该模块不支持 state={state}：{reason}。'
            if spec.date_field:
                lead = (f'（"即将到期"≈ {spec.date_field} 在未来 '
                        f'{spec.lead_days} 天内）') if spec.lead_days else ''
                hint += (f'如需按时间筛选，改用 date_from/date_to，'
                         f'日期字段是 {spec.date_field}{lead}。')
            else:
                hint += '请改用 keyword 或直接列出记录。'
        return None, registry.ToolResult(
            ok=False, error='BAD_ARGS', message=hint)
    return spec_state, None


def _scope_text(keyword='', state_spec=None):
    """统计范围的人话描述（回给模型，防止它把全表数字当条件结果）。"""
    bits = []
    if keyword:
        bits.append(f'筛选：{keyword}')
    if state_spec is not None:
        bits.append(f'状态：{state_spec.label}')
    return '；'.join(bits) if bits else '全表（未按条件筛选）'


@registry.tool(
    'query_records',
    '按模块查询记录列表，支持关键词、日期范围、状态、排序与条数限制。适合"列出/有哪些"类问题。'
    'status 类问题（已过期/即将到期/借出中/逾期）请用 state 参数，不要自己拼条件。',
    params=(
        registry.Param('module', 'module', required=True, description='模块 key'),
        registry.Param('keyword', 'string', maximum=60, description='关键词'),
        registry.Param('date_from', 'date', description='起始日期 YYYY-MM-DD'),
        registry.Param('date_to', 'date', description='截止日期 YYYY-MM-DD'),
        registry.Param('state', 'string', maximum=20,
                       description='业务状态：已过期/即将到期/借出中/已逾期/已归还'),
        registry.Param('order_by', 'string', maximum=40,
                       description='排序字段，前缀 - 表示倒序'),
        registry.Param('limit', 'int', default=20, minimum=1, maximum=MAX_ROWS),
    ),
    module_param=True,
    examples=('列出上个月的客户客诉', '保险今年到期的有哪些', '已过期的证照有哪些'),
)
def query_records(ctx, module, keyword='', date_from='', date_to='', state='',
                  order_by='', limit=20):
    spec = catalog.get_module(module)
    order = resolve_order(spec, order_by)
    if order is None:
        return registry.ToolResult(
            ok=False, error='BAD_ARGS',
            message=f'order_by 只能是 {list(spec.order_fields)} 之一',
        )
    state_spec, error = _state_of(spec, state)
    if error is not None:
        return error

    qs = apply_filters(base_queryset(spec, ctx.user), spec,
                       keyword=keyword, date_from=date_from, date_to=date_to,
                       state=state_spec)
    total = qs.count()
    objs = list(qs.order_by(order)[:limit])
    pol = ctx.policy
    rows = []
    for obj in objs:
        row, _ = project(spec, obj, spec.list_fields, pol)
        row['id'] = obj.pk
        rows.append(row)

    # 明细挂主档的模块（试样/小批量挂样品、审厂挂供应商）：把主档名称一并给出。
    # 否则「审厂没通过的供应商有哪些」只能回一串审厂编号，用户根本看不出是哪家。
    columns = labels(spec, spec.list_fields)
    if spec.parent_field:
        columns[spec.parent_field] = catalog.field_label(spec, spec.parent_field)
        for row, obj in zip(rows, objs):
            parent = getattr(obj, spec.parent_field, None)
            if parent is not None:
                row[spec.parent_field] = str(parent)[:40]

    refs = [catalog.build_ref(spec, obj) for obj in objs[:MAX_REFS]]
    data = {
        'module': spec.key,
        'module_label': spec.label,
        'total': total,
        'columns': columns,
        'date_field': catalog.field_label(spec, spec.date_field) if spec.date_field else '',
        'rows': rows,
    }
    if state_spec is not None:
        data['state'] = state_spec.label

    if total == 0:
        data['hint'] = _empty_hint(spec, ctx, date_from, date_to,
                                   state_spec.label if state_spec else '')
    return registry.ToolResult(
        ok=True,
        data=data,
        row_count=len(rows),
        truncated=total > len(rows),
        refs=refs,
    )


@registry.tool(
    'get_record',
    '按 id 查询单条记录详情（字段已按白名单裁剪并脱敏）。',
    params=(
        registry.Param('module', 'module', required=True),
        registry.Param('record_id', 'int', required=True, minimum=1),
    ),
    module_param=True,
    examples=('客户客诉 12 号那条具体什么情况',),
)
def get_record(ctx, module, record_id):
    spec = catalog.get_module(module)
    obj = base_queryset(spec, ctx.user).filter(pk=record_id).first()
    if obj is None:
        return registry.ToolResult(ok=False, error='NOT_FOUND',
                                   message='未找到该记录')
    row, _ = project(spec, obj, spec.detail_fields, ctx.policy, text_limit=300)
    row['id'] = obj.pk
    return registry.ToolResult(
        ok=True,
        data={'module': spec.key, 'module_label': spec.label,
              'columns': labels(spec, spec.detail_fields), 'record': row},
        row_count=1,
        refs=[catalog.build_ref(spec, obj)],
    )


@registry.tool(
    'aggregate_records',
    '按模块做统计：计数/求和/平均/最大/最小，可按枚举字段分组。适合"最多/多少/合计"类问题。'
    '统计前必须用 keyword 限定条件（客户名、状态、账期等），否则统计的是全表，'
    '不能把这个数字当成用户问题里条件的答案。'
    '一次只算一个指标；要同时得到"条数 + 金额合计"请分两次调用（先 count 再 sum）。'
    'sum/avg/max/min 必须给 field（金额等数值字段名）。',
    params=(
        registry.Param('module', 'module', required=True),
        registry.Param('metric', 'enum',
                       enum=('count', 'sum', 'avg', 'max', 'min'), required=True,
                       description='统计方式'),
        registry.Param('field', 'string', maximum=40, description='统计字段（count 可省）'),
        registry.Param('group_by', 'string', maximum=40, description='分组字段'),
        registry.Param('keyword', 'string', maximum=60,
                       description='筛选关键词（客户名/状态/账期等），不给则是全表'),
        registry.Param('state', 'string', maximum=20,
                       description='业务状态：已过期/即将到期/借出中/已逾期/已归还'),
        registry.Param('date_from', 'date'),
        registry.Param('date_to', 'date'),
        registry.Param('limit', 'int', default=20, minimum=1, maximum=50),
    ),
    module_param=True,
    examples=('上个月哪家客户客诉最多', '今年保险保费合计多少', '账期为月结60天的客户有多少'),
)
def aggregate_records(ctx, module, metric, field='', group_by='', keyword='',
                      state='', date_from='', date_to='', limit=20):
    spec = catalog.get_module(module)
    if metric != 'count':
        if not field:
            return registry.ToolResult(ok=False, error='BAD_ARGS',
                                       message=f'{metric} 必须指定 field')
        if field not in spec.sum_fields:
            return registry.ToolResult(
                ok=False, error='BAD_ARGS',
                message=f'field 只能是 {list(spec.sum_fields) or "（该模块无可求和字段）"} 之一',
            )
    if group_by and group_by not in spec.group_fields:
        return registry.ToolResult(
            ok=False, error='BAD_ARGS',
            message=f'group_by 只能是 {list(spec.group_fields) or "（该模块不支持分组）"} 之一',
        )
    state_spec, error = _state_of(spec, state)
    if error is not None:
        return error

    qs = apply_filters(base_queryset(spec, ctx.user), spec,
                       keyword=keyword, date_from=date_from, date_to=date_to,
                       state=state_spec)
    func = AGG_FUNCS[metric]
    agg = func(field) if metric != 'count' else func('id')

    rows = []
    if group_by:
        for item in qs.values(group_by).annotate(value=agg).order_by('-value')[:limit]:
            rows.append({
                catalog.field_label(spec, group_by): catalog.label_for_value(
                    spec, group_by, item[group_by]),
                'value': jsonable(item['value']),
            })
    else:
        rows.append({'value': jsonable(qs.aggregate(value=agg)['value'] or 0)})

    url = ''
    if spec.list_route:
        path = catalog.safe_reverse(spec.list_route)
        if path:
            query = {k: v for k, v in (('date_from', date_from), ('date_to', date_to)) if v}
            url = f'{path}?{urlencode(query)}' if query else path

    data = {
        'module': spec.key, 'module_label': spec.label,
        'metric': metric, 'field': field, 'group_by': group_by,
        'group_label': catalog.field_label(spec, group_by) if group_by else '',
        'rows': rows,
        # 把"这次到底统计了什么范围"原样回给模型：没有这一步，7B 会把全表的
        # 数字当成"用户条件筛选后的结果"（问月结60天多少家，答全表 50 家）。
        'keyword': keyword,
        'state': state_spec.label if state_spec else '',
        'scope': _scope_text(keyword, state_spec),
    }
    if not keyword and not state_spec and not date_from and not date_to:
        data['note'] = ('本次是全表统计，没有按用户问题里的条件筛选。'
                        '若问题带条件（客户名/状态/账期/日期等），请带 keyword 重新统计，'
                        '不要直接把这个数字当作答案。')
    return registry.ToolResult(
        ok=True,
        data=data,
        row_count=len(rows),
        refs=[{'module': spec.key, 'module_label': spec.label, 'id': None,
               'label': f'{spec.label}列表', 'url': url}] if url else [],
    )
