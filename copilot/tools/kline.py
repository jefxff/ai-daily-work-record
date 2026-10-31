"""时间序列工具：金属价格走势（K 线）。

为什么要单独一个工具，而不是让用户走 ``query_records``：

1. **维度不同**。普通查询返回"一条记录一行"；问"铜价最近三个月走势"的人
   要的是**按日期排好的一条序列**，还要首值/末值/涨跌/最值这类派生指标。
   让模型自己从几百行里算这些，既慢又容易算错。
2. **时间口径要有兜底**。小模型经常把"最近"理解成任意年份而查不到东西，
   所以这里既接受显式的 ``start``/``end``，也接受一句自然语言原文
   （``question``）自行抽取区间——两条路都没有时，才退到"近 90 天"，
   并且**明确告诉调用方用了默认值**，不偷偷替换口径。
"""
from decimal import Decimal

from django.db.models import Avg, Count, Max, Min, Q
from django.utils import timezone

from .. import catalog, registry
from .common import base_queryset
from .daterange import extract_date_range

MAX_POINTS = 120          # 取回参与计算的最大点数（超了再抽样）
DEFAULT_WINDOW_DAYS = 90  # 没给日期时的默认窗口
MAX_CANDIDATES = 8        # 名称歧义时最多返回几个候选

_COLUMNS = {
    'date': '日期', 'price': '均价', 'low': '最低', 'high': '最高',
    'samples': '当日条数',
}


def _today():
    """当天日期。**不要自己算时间**：时区由 Django 配置决定。"""
    return timezone.localdate()


def _to_date(value):
    """把字符串 / date 转成 date；失败返回 None。"""
    if not value:
        return None
    if hasattr(value, 'isoformat') and hasattr(value, 'year'):
        return value
    try:
        from datetime import date
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _resolve_range(start, end, question, today):
    """决定实际查询区间：显式优先 → 自然语言抽取 → 默认窗口。

    返回 ``(start_date, end_date, label, source)``。``source`` 有三种：
    ``explicit`` / ``natural_language`` / ``default_window``，原样回给调用方，
    让模型知道这次的口径是怎么来的。
    """
    start_date, end_date = _to_date(start), _to_date(end)
    if start_date and end_date and start_date <= end_date:
        return start_date, end_date, f'{start_date} ~ {end_date}', 'explicit'
    if start_date and end_date and start_date > end_date:
        start_date, end_date = end_date, start_date
        return start_date, end_date, f'{start_date} ~ {end_date}', 'explicit'

    parsed = extract_date_range(question or '', today=today)
    if parsed:
        # 显式给了一端时，只补另一端：用户说"2024 年之后"，不能把 3 月前的也塞进来
        start_date = start_date or parsed.start
        end_date = end_date or parsed.end
        if start_date and end_date and start_date <= end_date:
            return start_date, end_date, parsed.label, 'natural_language'

    if not start_date and not end_date:
        end_date = today
        start_date = today - timezone.timedelta(days=DEFAULT_WINDOW_DAYS)
        return start_date, end_date, (
            f'{start_date} ~ {end_date}（未指定日期，默认近 {DEFAULT_WINDOW_DAYS} 天）'
        ), 'default_window'
    # 只给了一端：另一端不设限
    return start_date, end_date, (
        f'{start_date or "不限"} ~ {end_date or "不限"}'), 'explicit'


def _period_key(date_value, interval):
    """按粒度把日期压成分组键。"""
    if interval == 'weekly':
        iso = date_value.isocalendar()
        return f'{iso[0]}-W{iso[1]:02d}'
    if interval == 'monthly':
        return f'{date_value.year}-{date_value.month:02d}'
    return date_value.isoformat()


def _downsample(rows, interval, limit):
    """按粒度合并；点数仍超过上限就等距抽样（首末必留）。

    等距而不是"截前 N 条"：截前 N 条会把"最近走势"显示成"最开始那段"，
    结论正好相反。
    """
    grouped = []
    if interval in ('weekly', 'monthly'):
        buckets = {}
        for row in rows:
            key = _period_key(row['date'], interval)
            if key not in buckets:
                buckets[key] = {'date': key, 'low': row['low'], 'high': row['high'],
                                'sum': 0.0, 'samples': 0, 'count': 0}
            bucket = buckets[key]
            bucket['low'] = min(bucket['low'], row['low'])
            bucket['high'] = max(bucket['high'], row['high'])
            bucket['sum'] += row['price'] * row['samples']
            bucket['samples'] += row['samples']
            bucket['count'] += 1
        for key in sorted(buckets):
            bucket = buckets[key]
            grouped.append({
                'date': key,
                'price': round(bucket['sum'] / max(bucket['samples'], 1), 2),
                'low': _num(bucket['low']),
                'high': _num(bucket['high']),
                'samples': bucket['samples'],
            })
    else:
        grouped = [{
            'date': row['date'].isoformat(),
            'price': _num(row['price']),
            'low': _num(row['low']),
            'high': _num(row['high']),
            'samples': row['samples'],
        } for row in rows]

    if len(grouped) <= limit:
        return grouped, False
    if limit <= 2:
        return [grouped[0], grouped[-1]][:limit], True
    step = (len(grouped) - 1) / (limit - 1)
    picked = [grouped[round(i * step)] for i in range(limit)]
    return picked, True


def _num(value):
    """Decimal → float，便于 JSON 序列化。None 原样返回。"""
    if value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def _summarize(series):
    """走势摘要：首值、末值、涨跌、最值、均值。"""
    prices = [point['price'] for point in series if point.get('price') is not None]
    if not prices:
        return {}
    first, last = prices[0], prices[-1]
    change = round(last - first, 2)
    return {
        'first': first, 'last': last, 'change': change,
        'change_pct': round(change / first * 100, 2) if first else 0.0,
        'min': min(prices), 'max': max(prices),
        'avg': round(sum(prices) / len(prices), 2),
        'points': len(prices),
    }


@registry.tool(
    'search_kline',
    '查询金属价格的时间序列走势（按日/周/月聚合，返回序列与涨跌摘要）。'
    '日期区间三种给法：① 显式 start/end；② 在 question 里写自然语言'
    '（"2019到2021""最近30天""2024年3月~6月"），工具自动抽取；'
    '③ 都不给则默认近 90 天并在结果里注明。'
    '单价.unit、symbol 支持模糊匹配（"铜"能命中"电解铜"）。',
    params=(
        registry.Param('symbol', 'string', required=True, maximum=40,
                       description='金属名称关键词，如 铜/铝/电解铜'),
        registry.Param('question', 'string', maximum=120,
                       description='原始问句：用于抽取自然语言日期区间'),
        registry.Param('start', 'date', description='起始日期 YYYY-MM-DD'),
        registry.Param('end', 'date', description='截止日期 YYYY-MM-DD'),
        registry.Param('interval', 'enum',
                       enum=('raw', 'daily', 'weekly', 'monthly'), default='raw',
                       description='聚合粒度：raw/daily 按天，weekly 按周，monthly 按月'),
        registry.Param('limit', 'int', default=60, minimum=1, maximum=MAX_POINTS,
                       description='返回点数上限（超上限时等距抽样并保留首末）'),
    ),
    module_key='metal_price',
    examples=('2019 到 2021 的铜价走势', '最近三个月铝价怎么样', '2024年三季度铜价'),
)
def search_kline(ctx, symbol='', question='', start='', end='',
                 interval='raw', limit=MAX_POINTS):
    if not symbol or not str(symbol).strip():
        return registry.ToolResult(
            ok=False, error='BAD_ARGS',
            message='symbol 不能为空：请给出要查的金属名称（如 铜、铝、电解铜）。')

    spec = catalog.get_module('metal_price')
    today = _today()
    start_date, end_date, label, source = _resolve_range(start, end, question, today)

    date_field = spec.date_field or 'crawl_date'
    keyword = str(symbol).strip()
    qs = base_queryset(spec, ctx.user).filter(
        Q(metal_name__icontains=keyword) | Q(unit__icontains=keyword))
    # 品名消歧义：一个关键词命中多个品种时必须让用户先选，
    # 不同金属之间价差可达百倍，混在一起算均值毫无意义。
    names = list(qs.values_list('metal_name', flat=True).distinct()[:50])
    exact = [name for name in names if keyword.lower() == (name or '').lower()]
    if not exact and len(names) > 1:
        # 名字太多时必须让用户先选：不同金属的价差可达百倍，混着算没意义
        candidates = list(names[:MAX_CANDIDATES])
        return registry.ToolResult(
            ok=False, error='AMBIGUOUS_SYMBOL',
            message=(f'「{keyword}」匹配到 {len(names)} 个品名：'
                     f'{ "、".join(candidates) }。'
                     '请用更精确的品名（或改用 query_records 看完整列表）后再查走势。'),
        )

    if start_date:
        qs = qs.filter(**{f'{date_field}__gte': start_date})
    if end_date:
        qs = qs.filter(**{f'{date_field}__lte': end_date})

    total = qs.count()
    if total == 0:
        latest = None
        try:
            latest = (base_queryset(spec, ctx.user)
                      .order_by(f'-{date_field}')
                      .values_list(date_field, flat=True).first())
        except Exception:  # noqa: BLE001 - 查最近日期失败只影响提示文案
            latest = None
        hint = f'「{keyword}」在 {label} 内没有价格记录。'
        if latest:
            hint += f'该模块最新一条的日期是 {latest}，请据此调整日期区间后重试。'
        return registry.ToolResult(
            ok=True, data={'symbol': keyword, 'series': [], 'range': {
                'start': start_date.isoformat() if start_date else None,
                'end': end_date.isoformat() if end_date else None,
                'label': label, 'source': source},
                'hint': hint},
            row_count=0, message=hint,
        )

    rows = list(
        qs.values(date_field)
        .annotate(price=Avg('price'), low=Min('price'), high=Max('price'),
                  samples=Count('id'))
        .order_by(date_field)[:MAX_POINTS * 4]
    )
    for row in rows:
        row['date'] = row.pop(date_field)

    series, truncated = _downsample(rows, interval or 'raw', limit or MAX_POINTS)
    unit_rows = qs.exclude(unit='').values_list('unit', flat=True).distinct()[:3]
    data = {
        'symbol': exact[0] if exact else (names[0] if names else keyword),
        'unit': '、'.join(unit_rows),
        'interval': interval or 'raw',
        'columns': _COLUMNS,
        'series': series,
        'summary': _summarize(series),
        'range': {
            'start': start_date.isoformat() if start_date else None,
            'end': end_date.isoformat() if end_date else None,
            'label': label,
            'source': source,
        },
        'notes': '日期口径来源：' + {
            'explicit': '调用方显式指定',
            'natural_language': '从原始问句抽取',
            'default_window': f'未指定，默认近 {DEFAULT_WINDOW_DAYS} 天',
        }.get(source, source),
    }
    if truncated:
        data['hint'] = (f'区间内共 {len(rows)} 个点，已等距抽样到 {len(series)} 个'
                        '（首末保留）；需要完整序列请加粗粒度或缩小日期范围。')
    return registry.ToolResult(
        ok=True, data=data, row_count=len(series), truncated=truncated,
    )
