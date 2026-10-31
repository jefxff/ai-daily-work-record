"""上下文与会话历史组装、闲聊快问快答，以及模型不可用时的关键词降级。"""

import ast
import re
from datetime import date, timedelta

from django.db.models import Count
from django.utils import timezone

from .. import catalog, registry, security
from ..tools.common import apply_filters, base_queryset, project, resolve_order
from . import prompt

MAX_HISTORY = 4
HISTORY_CHAR_LIMIT = 600

# 降级检索的上限：条数太少等于没结果，太多则把面板刷屏
MAX_FALLBACK_REFS = 10
PER_MODULE_HITS = 3

_NOISE_RE = re.compile(r'[的了吗呢？?，,。.！!、\s]+$')
# 疑问/动词切分：用它们把问句切成若干"名词片段"，降级检索时逐段尝试。
# 例："华建建材有哪些客户客诉" → ["华建建材", "客户客诉"]
_SPLIT_RE = re.compile(
    r'(?:有哪些|哪些|多少个|多少|怎么样|如何|是什么|什么|请问|帮我|帮忙|查一下|查询|'
    r'统计|列出|看看|看一下|一下|有没有|是否|的|了|吗|呢)'
)
# 时间性问句：命中后认为用户想要"最新的几条"，而不是某条具体记录
_RECENT_RE = re.compile(r'最近|最新|近期|这几天|本周|本月|上周|昨天|今天|新发布')

# 模块别名：问句里不一定会出现完整 label（"制度" vs "制度及处罚"），
# 这里补上 label 覆盖不到的口语说法。新增模块时同步补一条即可。
#
# 2026-10-31：档案/证照/借阅/知识库整块没接入，用户怎么问都识别不出来。
# 接入时顺手把所有模块的口语说法补齐——"资质""营业执照""年检""归档"
# 这些词用户天天说，但都不是任何模块的 label。
MODULE_ALIASES = {
    'customer_complaint': ('客诉', '投诉', '客户投诉', '客诉记录'),
    'complaint': ('诉讼', '起诉', '案件', '官司', '诉讼案件', '被告'),
    'insurance': ('保险', '保单', '保费', '续保', '保险单', '投保'),
    'customer_ledger': ('台账', '客户档案', '客户信息', '客户资料', '客户清单'),
    'customer_credit': ('账期', '信用额度', '授信', '账期申请', '额度申请'),
    'decoration': ('装修', '装修项目', '工程装修'),
    'daily_record': ('工作记录', '日报', '日志', '每日', '工作日报', '日记'),
    'regulation': ('制度', '规定', '管理办法', '处罚', '规章制度', '条例'),
    'price_evaluation': ('核价', '比价', '询价', '核价单', '核价结果'),
    'metal_price': ('金属', '铜价', '铝价', '钢价', '金属价格', '有色金属'),
    'sample': ('样品', '送样', '打样', '样品送样', '来样'),
    'sample_test': ('试样', '试产', '测试记录', '试样试产', '打样测试'),
    'sample_batch': ('小批量', '批次', '小批量试产', '批量验证'),
    'sample_audit': ('审厂', '验厂', '审厂记录', '工厂审核'),
    'sample_partner': ('供应商', '厂商', '合作伙伴', '供应商档案'),
    'archive': ('档案', '合同档案', '档案资料', '归档', '合同'),
    'archive_borrow': ('档案借阅', '借阅记录', '借阅', '借出记录', '归还记录'),
    'license': ('证照', '资质', '证书', '营业执照', '许可证', '资质证书'),
    'license_review': ('年审', '证照年审', '年审记录', '年检'),
    'license_borrow': ('证照借阅', '证照借出', '证照归还', '证照借用'),
    'knowledge': ('知识库', '知识', '文档', '知识文档', '资料库', 'sop'),
}


# 闲聊/问候：这类问题不需要任何数据，交给模型只会让它去"查点什么"。
# 小模型尤其容易反复调 list_modules 直到轮次触顶，用户最后只看到一句
# "超出单次上限"——所以问候在本地直接答，连模型都不必调。
_PUNCT_RE = re.compile(r"[\s，,。.！!？?~～、…：:；;\"'“”‘’()（）\[\]【】]+")
_TAIL_RE = re.compile(r'(?:呀|啊|哈|哦|呢|啦|吧|嘛|哟)+$')
_SMALLTALK = frozenset({
    '你好', '您好', '你们好', '大家好', '哈喽', '哈啰', '嗨', 'hi', 'hello', 'hey',
    '在吗', '在不在', '有人在吗', '早上好', '上午好', '中午好', '下午好', '晚上好',
    '早安', '晚安', '你是谁', '你叫什么', '你叫什么名字', '你能做什么',
    '你会做什么', '你可以做什么', '你有什么功能', '有什么功能', '帮助', 'help',
    '测试', 'test', '谢谢', '谢谢你', '谢了', '多谢', '感谢', '再见', '拜拜', 'bye',
})
_THANKS = frozenset({'谢谢', '谢谢你', '谢了', '多谢', '感谢'})
_BYE = frozenset({'再见', '拜拜', 'bye'})


def normalize_smalltalk(question):
    """去掉标点、空格与语气词，便于与固定词表比对。"""
    text = _PUNCT_RE.sub('', (question or '').strip()).lower()
    return _TAIL_RE.sub('', text)


def is_smalltalk(question):
    return normalize_smalltalk(question) in _SMALLTALK


def quick_reply(question, modules, blocked=()):
    """纯问候/道谢/问能力：本地直接答，返回空串表示"不是闲聊"。"""
    core = normalize_smalltalk(question)
    if core not in _SMALLTALK:
        return ''
    if core in _THANKS:
        return '不客气。还需要查什么，直接问我就行。'
    if core in _BYE:
        return '好的，需要时再叫我。'

    return capability_reply(modules, blocked)


def capability_reply(modules, blocked=()):
    """"你能做什么 / 能查哪些模块"：模块清单是确定的，本地答即可。"""
    lines = ['你好，我是站内 AI 助手，可以直接用中文问业务数据，例如：']
    lines.extend(f'- {q}' for q in prompt.suggested_questions(modules)[:3])
    # 模块清单一次给全：以前只列前 16 个，档案/证照/知识库刚好被截掉，
    # 用户照着清单问都问不到，还以为系统不支持。
    labels = '、'.join(m.label for m in modules)
    if labels:
        lines.append(f'我能查的模块：{labels}。')
    else:
        lines.append('当前账号还没有可查询的业务模块权限，请联系管理员开通。')
    if blocked:
        # 云端模型下敏感模块不可用：问候里也别许诺查不到的东西
        lines.append('（当前使用云端模型，'
                     + '、'.join(m.label for m in blocked[:5])
                     + ' 的数据不允许外发，暂时查不到。）')
    return '\n'.join(lines)


# "今天几号 / 现在是哪一天 / 今天星期几"：答案是确定值，模型只会答错或跑去查库。
# 截图里用户问「今天是哪一天」，7B 拿"今天"当关键词做跨表搜索 → 0 条 →
# 重复调用熔断 → 「没能把结果整理成回答」。这类问题根本不该进模型。
_DATE_RE = re.compile(
    r'(今天|今日|昨天|昨日|前天|明天|明日|后天)[^，,。？?！!\n]{0,6}'
    r'(几号|几月几[日号]|多少号|日期|哪一天|哪天|星期几|周几|礼拜几)'
)
_TIME_RE = re.compile(r'(现在|当前|此刻)[^，,。？?！!\n]{0,4}(几点|时间|时刻)')
_DAY_OFFSET = {'今天': 0, '今日': 0, '昨天': -1, '昨日': -1, '前天': -2,
               '明天': 1, '明日': 1, '后天': 2}
_WEEKDAYS = '一二三四五六日'

# "能查哪些模块 / 支持查询什么"：也本地答，别让模型去调 list_modules 绕圈
_CAPABILITY_RE = re.compile(
    r'(能|可以|支持|会)[^，,。？?！!\n]{0,4}(查|检索|看|问)[^，,。？?！!\n]{0,4}(什么|哪些|啥)'
    r'|(有|支持)(哪些|那些|什么)[^，,。？?！!\n]{0,3}模块'
)


def date_reply(question):
    """日期/星期/时间类问题本地直答，返回空串表示不是这类问题。"""
    text = question or ''
    matched = _DATE_RE.search(text)
    if matched:
        day = timezone.localdate() + timedelta(days=_DAY_OFFSET[matched.group(1)])
        return (f'{matched.group(1)}是 {day.year}年{day.month}月{day.day}日，'
                f'星期{_WEEKDAYS[day.weekday()]}。')
    if _TIME_RE.search(text):
        return f'现在是 {timezone.localtime().strftime("%Y-%m-%d %H:%M")}。'
    return ''


# “1+1等于几”这种纯算式：答案确定，可模型会为了“查点什么”去跑两轮跨模块
# 搜索（截图里正是 0 条 ×2 + 重复调用已跳过），最后还提示步骤异常。
# 算式在本地算完直接给，既快又不给用户看一堆无效步骤。
_MATH_EXPR_RE = re.compile(r'^[\d\s.+\-*/×÷()（）]+$')
_MATH_TAIL_RE = re.compile(
    r'(?:等于多少|等于几|等于|是多少|多少|几|吧|呢|呀|啊|=|＝|\?|？|。|，|,)+$')
_MATH_OPS = '+-*/×÷'
_DATE_LIKE_RE = re.compile(r'\d{4}-\d{1,2}-\d{1,2}')


def _eval_math(node):
    """只认数字与四则运算，不 exec、不 eval，杜绝任何代码执行。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp):
        operand = _eval_math(node.operand)
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return operand
        raise ValueError('unsupported unary op')
    if isinstance(node, ast.BinOp):
        left, right = _eval_math(node.left), _eval_math(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        raise ValueError('unsupported operator')
    raise ValueError('unsupported expression')


def math_reply(question):
    """纯四则算式本地直算，返回空串表示“不是算式”。"""
    text = (question or '').strip()
    expr = _MATH_TAIL_RE.sub('', text).strip()
    if not expr or not _MATH_EXPR_RE.match(expr):
        return ''
    if not any(op in expr for op in _MATH_OPS):
        return ''
    # “2026-10-31”长得也像算式，别算成 1985
    if _DATE_LIKE_RE.fullmatch(expr):
        return ''
    normalised = (expr.replace('×', '*').replace('÷', '/')
                  .replace('（', '(').replace('）', ')'))
    try:
        value = _eval_math(ast.parse(normalised, mode='eval').body)
    except (SyntaxError, ValueError, ZeroDivisionError, TypeError):
        return ''
    if value is None:
        return ''
    shown = int(value) if float(value).is_integer() else round(float(value), 6)
    return f'{expr} = {shown}。'


def instant_reply(question, modules, blocked=()):
    """不需要任何数据就能确定回答的问题：闲聊、日期时间、能力清单。

    统一在这里拦住，模型不参与——它参与只会"为了查点什么"去调工具，
    最后以触顶或"没能整理成回答"收场。
    """
    quick = quick_reply(question, modules, blocked)
    if quick:
        return quick
    calculated = math_reply(question)
    if calculated:
        return calculated
    replied = date_reply(question)
    if replied:
        return replied
    if _CAPABILITY_RE.search(question or ''):
        return capability_reply(modules, blocked)
    return ''



def history_messages(conversation, limit=MAX_HISTORY):
    """取最近若干轮对话（只取 user/assistant 的正文，不带工具结果）。"""
    rows = list(
        conversation.messages
        .filter(role__in=['user', 'assistant'])
        .exclude(content='')
        .order_by('-id')[:limit]
    )
    return [
        {'role': m.role, 'content': m.content[:HISTORY_CHAR_LIMIT]}
        for m in reversed(rows)
    ]


def clean_keyword(question):
    text = _NOISE_RE.sub('', (question or '').strip())
    return text[:30]


def keyword_candidates(question):
    """把问句拆成若干候选关键词，供降级检索逐段尝试。

    整句在 icontains 下几乎不可能命中（"华建建材有哪些客户客诉"不是任何字段
    的连续子串），所以必须拆。"华建建材"能命中客户名，就够给用户一个入口了。
    """
    text = _NOISE_RE.sub('', (question or '').strip())
    candidates = [text[:30]]
    for chunk in _SPLIT_RE.split(text):
        chunk = (chunk or '').strip()
        # 纯时间词（最近/本周…）当关键词检索没有意义，反而会误命中
        if 2 <= len(chunk) <= 20 and not _RECENT_RE.fullmatch(chunk):
            candidates.append(chunk)
    return [c for c in dict.fromkeys(candidates) if len(c) >= 2]


def hinted_modules(question, modules):
    """问题里点名的模块（按 label / 别名匹配）。

    "最近发布了哪些制度" 里没有任何字段值可以匹配，但用户显然是在问制度模块；
    认出模块才能退而求其次地给出"该模块最新的几条"。
    """
    text = question or ''
    hits = []
    for spec in modules:
        words = MODULE_ALIASES.get(spec.key, ()) + (spec.label,)
        if any(word and word in text for word in words):
            hits.append(spec)
    return hits


# 判断"除了模块名以外还有没有实词"时剥掉的虚词/动作词
_INTENT_STOP_RE = re.compile(
    r'(?:有哪些|有那些|有些什么|有没有|有么有|有哪些记录|是否|能不能|可不可以|'
    r'记录|列表|清单|明细|数据|查一下|查询|查看|'
    r'看看|列出|统计|一共|总共|多少条|多少个|多少|几条|所有|全部|分别|以及|还有|'
    r'里面|里边|中的|管理|系统|模块|给我|帮我|想看|我要|请问|一下|的|了|吗|呢|中|里|'
    r'和|与|及|我|你)'
)
# 列表语义：没有这些词时，"客诉详情"这类问句应交给模型而不是降级成列表
_LIST_CUE_RE = re.compile(
    r'(有哪些|有那些|有些什么|记录|列表|清单|明细|全部|所有|一共|总共|多少条|多少个|'
    r'几条|统计|列出|看看)'
)


def _own_core(text, spec):
    """只剥该模块自己的名字与虚词，保留其它模块名（可能是真正的内容关键词）。

    「知识库里有没有客诉处理流程」里的"客诉"是**检索词**，不是"用户要查客诉
    模块"。按"剥掉所有模块名"来算关键词，会把检索词一起剥掉，搜出空结果。
    """
    rest = text or ''
    for word in (spec.label,) + tuple(MODULE_ALIASES.get(spec.key, ())):
        if word:
            rest = rest.replace(word, '')
    return _PUNCT_RE.sub('', _INTENT_STOP_RE.sub('', rest))


def module_query(question, modules, user=None):
    """「就是想看某个模块的记录」时返回 ``(spec, keyword, state)``，否则 ``(None, '', None)``。

    「试样管理中 试样中的记录有那些」这类问句，模型会拿"试样"当关键词跨表搜，
    可"试样"是模块名、不是任何字段的值，必然 0 条——于是答成"没有相关记录"，
    把"不会查"说成了"没有"。剥掉模块名与虚词后若没剩下实词，说明用户要的
    就是该模块的列表，直接本地查最快也最准。

    剩下的若是选项词（"没通过""待测试"）、库中真实存在的值（"月结60天"），
    或业务状态（"已过期""已借出""逾期"），也一样接管：这些条件在库中可验证，
    交给模型只会搜不到或筛错。

    ``state`` 是 ``catalog.StateSpec``：状态不是字段（证照"已过期"= 有效期或
    年审日已过），必须整段交给 ``apply_filters`` 才能筛对。

    剩下"华建建材"这类实词、或只是"客诉详情"这种指向不明的问题时返回 None：
    交给模型判断，避免把明细查询降级成列表。
    """
    text = question or ''
    spec = pick_module(text, modules, user)
    if spec is None:
        # 没点名模块时，看问题里的值是否唯一落在某个模块上：
        # 「月结60天的客户有那些」以前谁都接不住，最后让模型把一条记录贴出来。
        if not (_LIST_CUE_RE.search(text) or _COUNT_RE.search(text)):
            return None, '', None
        landed = [(m, value) for m in modules
                  for field, value in [db_value_match(m, text, user)] if field]
        if len(landed) != 1:
            return None, '', None
        matched, value = landed[0]
        return matched, value, _state_of_text(matched, text)

    state = _state_of_text(spec, text)

    # 同一字段出现两个选项词（"立案…结案"）= 要按该字段分组，交给 stat_query
    if any(len(found) >= 2 for found in matched_labels(spec, text).values()):
        return None, '', None

    rest = text
    for other in modules:
        for word in (other.label,) + tuple(MODULE_ALIASES.get(other.key, ())):
            if word:
                rest = rest.replace(word, '')
    rest = _PUNCT_RE.sub('', _INTENT_STOP_RE.sub('', rest))
    if len(rest) <= 2:
        # "客诉详情"这类没带列表语义的问题，别降级成"列出全部"
        if rest and not _LIST_CUE_RE.search(text) and not state:
            return None, '', None
        return spec, '', state
    # 知识库是"文档检索"：剩下的词就是全文关键词，直接本地搜（标题/摘要/正文都搜）
    if spec.key == 'knowledge':
        own = _own_core(text, spec)
        return spec, (own[:30] if len(own) >= 2 else ''), state
    # 剩下的若是选项词（"没通过"→fail），可以确定性地当筛选条件用
    from ..tools.common import _choice_conditions   # 延迟导入，避免循环
    if _choice_conditions(spec, rest):
        return spec, rest, state
    # 或是库中真实存在的值（"月结60天""华建建材"）——同样是可验证的条件
    _field, value = db_value_match(spec, text, user)
    if value:
        return spec, value, state
    # 剩下的只是状态词（"已过期""已借出"）：状态本身就是筛选条件
    if state is not None:
        return spec, '', state
    return None, '', None


def module_intent(question, modules, user=None):
    """兼容旧调用：只关心"是哪个模块"时用它。"""
    spec, _keyword, _state = module_query(question, modules, user)
    return spec




# 统计类问题的确定信号："一共多少 / 几条 / 各有几个"。刻意不用裸"多少"：
# 「今日铜价是多少」问的是取值，当成计数会答成一堆条数。
_COUNT_RE = re.compile(
    r'(多少条|多少家|多少个|多少笔|多少件|多少位|多少名|多少起|多少台|多少种|'
    r'多少份|多少张|多少篇|多少项|多少宗|多少套|多少批|多少本|多少册|多少次|'
    r'有几条|有几家|有几个|有几件|有几次|有几份|'
    r'几条|几家|几个|几笔|几件|几起|几种|几份|几张|几篇|几次|'
    r'有多少|'
    r'一共|总共|合计|总计|数量|统计|'
    r'分别有|分别多少|各有多少)'
)
# 分组信号："按客户分类分组""各状态分别有多少"。
# 光靠"同一字段出现两个选项标签"识别不到这种问法——用户只说了一个分类词，
# 却明确要求"按它拆开看"，这就是要 group_by。
_GROUP_CUE_RE = re.compile(r'(分别|各组|各有多少|各有几|分组|分类统计|按[^，,。？?]{0,8}(分组|统计|汇总|看))')
# 带时间范围的问题不本地统计：本月/上月/今年究竟对应哪个日期字段，各模块语义
# 不同，猜错就会把"全表"的数字说成"本月"的——宁可交给模型，也不能答错。
_TIME_SCOPE_RE = re.compile(
    r'(本月|这个月|当月|上月|上个月|去年|今年|本年|年初|年底|本周|这周|上周|下周|'
    r'最近|近期|近\d+|过去\d+|昨天|今天|明天|季度)'
)
# 明确说了年/月/季度时，不能把状态问题简化成"未来 30 天"：
# 「今年有哪些保险要到期」≠「30 天内到期」。这类问题交回模型按日期区间处理。
_DATE_SCOPE_RE = re.compile(
    r'(本月|这个月|当月|上月|上个月|去年|今年|本年|年初|年底|季度|近\d+|过去\d+|'
    r'\d{4}年)'
)

# 量词：客户数说"家"、诉讼说"件"，一律叫"条"读起来不像人话
_UNIT_MAP = {'customer_ledger': '家', 'customer_credit': '家',
             'sample_partner': '家', 'complaint': '件', 'insurance': '份',
             'archive': '份', 'license': '份', 'knowledge': '篇',
             'license_review': '次'}


def unit_of(spec):
    return _UNIT_MAP.get(spec.key, '条')


def _state_of_text(spec, text):
    """问题里的业务状态（已过期/借出中…），带年份月份时返回 None。

    状态筛选是"截至今天"的快照；用户说了"今年/本月"就得按日期区间算，
    简化成截止今天会把"今年到期"答成"30 天内到期"。
    """
    if spec is None or _DATE_SCOPE_RE.search(text or ''):
        return None
    return catalog.state_match(spec, text)


def matched_labels(spec, text):
    """问题里出现的该模块选项标签，按字段归组：``{字段: [标签, …]}``。

    同时出现同一字段的两个标签（"立案…结案"）就是"要按这个字段分组"的信号，
    这是 ``group_by`` 最可靠的来源——比让模型自己挑字段准得多。
    """
    out = {}
    for name, choices in catalog.choice_fields(spec):
        # 单字标签（"有"/"无"）不能当信号：任何一句中文都可能含"有"，
        # 会把"客户台账"误判成"客户账期"（pre_survey 有/无）。
        found = [label for _value, label in choices
                 if label and len(label) >= 2 and label in text]
        # 选项标签互为子串时只认长的那个："有条件通过"里也含"通过"，
        # 两个都算就会被当成"同一字段出现两个标签"→ 误判为分组。
        found = [label for label in found
                 if not any(label != other and label in other for other in found)]
        if found:
            out[name] = found
    return out


def _value_fields(spec):
    """允许"按值反查"的字段：选项字段 + 分组维度 + 标题字段（都是短文本分类信息）。"""
    text_fields = set(catalog.search_fields(spec))
    names = [name for name, _choices in catalog.choice_fields(spec)]
    for name in list(spec.group_fields) + ([spec.title_field] if spec.title_field else []):
        if name and name not in names:
            names.append(name)
    return [n for n in names if n in text_fields]


def db_value_match(spec, text, user=None, max_values=300):
    """问题里出现的、确实存在于库中的字段值：``(字段, 值)``，否则 ``(None, '')``。

    为什么不靠"剥掉虚词后剩下的片段"：``"账期为月结60天的客户"`` 剥完只剩"客户"，
    哪个字段都匹配不上，于是模型只能猜。反过来向库要"你有哪些取值"，就能确定
    ``payment_terms='月结60天'``——这是**可在库中验证**的筛选条件，不是猜的。

    只扫分类字段（选项/分组/标题）：基数小、本身就是要被筛选的维度；长文本描述
    不参与（把整段描述拿来比对既慢又没意义）。
    """
    best = (None, '')
    qs = base_queryset(spec, user)
    for name in _value_fields(spec):
        try:
            values = list(qs.values_list(name, flat=True).distinct()[:max_values])
        except Exception:  # noqa: BLE001 - 字段异常不能影响问答
            continue
        for value in values:
            value = str(value or '').strip()
            if 2 <= len(value) <= 30 and value in text and len(value) > len(best[1]):
                best = (name, value)
    return best


# 泛化后缀：任何模块都可能被叫成"XX台账 / XX记录 / XX清单"。
# 「样品台账中小批量的有哪些」里，"台账"只是通用后缀，可它恰好是客户台账的
# 别名——按长度+位置打分时，"样品"(2 字) 与"台账"(2 字) 打平，能不能选中
# 小批量全靠运气。泛词必须降权：命中具体业务词的模块才配当主语。
GENERIC_NAME_WORDS = frozenset({'台账', '记录', '清单', '明细', '数据', '列表'})


def _name_hit(spec, text):
    """问题里命中的最具体模块名：``(匹配起点, 长度, 具体词长度)``。

    用于区分"档案借阅"（档案借阅模块）与"档案"（档案模块）、"证照借阅"
    与"借阅记录"：都命中时，**更长的词**才算用户真正在说的那个模块；
    长度相同再比起始位置（"证照借阅记录"里"证照借阅"从头开始，说明它才是主语）。

    第三个返回值是"非泛词"的长度：只命中"台账""记录"这类通用后缀时为 0，
    排序时排在命中"样品""小批量"等具体词的模块之后。
    """
    words = MODULE_ALIASES.get(spec.key, ()) + (spec.label,)
    best = (len(text) + 1, 0, 0)
    for word in words:
        if not word:
            continue
        index = text.find(word)
        if index < 0:
            continue
        specific = 0 if word in GENERIC_NAME_WORDS else len(word)
        if (len(word), -index, specific) > (best[1], -best[0], best[2]):
            best = (index, len(word), specific)
    return best


def _module_score(spec, text, user=None):
    """判断题里点到的多个模块，哪个才是用户真正在问的。

    先看选项词（“没通过”“立案”），再看字段值（“月结60天”能落到谁的值域里），
    最后才比模块名：具体业务词优先于“台账/记录”这类泛称，再往下比长短与位置。
    """
    labels = matched_labels(spec, text)
    value_field, _value = db_value_match(spec, text, user)
    evidence = sum(len(v) for v in labels.values()) * 2 + (1 if value_field else 0)
    start, length, specific = _name_hit(spec, text)
    return (evidence, specific, length, -start)


def pick_module(text, modules, user=None):
    """问句里的模块若只有一个，直接返回；有多个时按"选项词/字段值/名字具体度"择优。

    名字命中有可能是假命中：「账期是月结90天的客户有几家」只提到"账期"，
    会落到"客户账期"（账期申请），而"月结90天"其实是客户台账的字段值。
    所以名字命中之后还要看有没有**可验证的证据**（选项词/字段值）；一条证据
    都没有时，才回头相信名字，并允许另一个模块用更具体的名字把它顶掉。
    """
    hinted = hinted_modules(text, modules)
    best_hint = None
    if hinted:
        # 稳定排序：证据相同、名字一样具体时保持 catalog 声明顺序
        best_hint = sorted(hinted, key=lambda s: _module_score(s, text, user),
                           reverse=True)[0]
        # 有可验证证据（选项词/字段值）就直接信它，不必再扫全表值域
        if _module_score(best_hint, text, user)[0] > 0:
            return best_hint

    # 名字命中但拿不出证据时，允许别的模块用"库中真实存在的值"把它顶掉：
    # 「账期是月结90天的客户有几家」只提到"账期"（→ 客户账期），
    # 可"月结90天"是客户台账的字段值——数字只能从台账里算。
    landed = []
    for spec in modules:
        field, value = db_value_match(spec, text, user)
        if field:
            # 命中"标题"这种自由文本只算弱证据：别的模块一条记录恰好叫
            # 「装修工程记录」，就能把「装修工程记录」这问句抢走——
            # 名字已经点名了模块时，只有可枚举的业务值才配改判。
            weak = bool(spec.title_field) and field == spec.title_field
            landed.append((len(value), spec, weak))
    if best_hint is not None:
        _start, name_len, specific = _name_hit(best_hint, text)
        if specific > 0:
            # 两条硬要求，缺一个就会误判：
            # 1) 不能是自由文本标题——别的模块一条记录叫「装修工程记录」就能抢走问句；
            # 2) 值要比模块名更长——「试样测试结果」里的 department='测试' 只是词的一部分，
            #    信息量不如"试样"本身，没资格改判。
            landed = [item for item in landed
                      if not item[2] and item[0] > name_len]
    if landed:
        landed.sort(key=lambda item: -item[0])
        # 值同时落在多个模块上（"华建建材"既是客户也是供应商）就不猜，交给模型
        if len(landed) == 1 or landed[0][0] > landed[1][0]:
            return landed[0][1]
    return best_hint


def stat_query(question, modules, user=None):
    """计数/分组类问题的确定性意图，命中返回 dict，否则 ``None``。

    这类问题的答案是**算出来的**，不是"看出来的"。让 7B 自己决定统计口径，
    它会把全表的数字当成筛选后的结果：截图上问「账期为月结60天的客户一共有
    多少」，它调了 ``aggregate_records(group_by=customer_type)`` 却没带任何
    筛选条件，然后把全表的 50 家（普通 32 + 代加工 18）当成了答案——数字全真，
    结论全错。能算就别问模型。
    """
    text = question or ''
    if not text.strip() or _TIME_SCOPE_RE.search(text):
        return None
    spec = pick_module(text, modules, user)
    if spec is None:
        return None

    labels = matched_labels(spec, text)
    group_field = next((name for name, found in labels.items() if len(found) >= 2), '')
    if not group_field and _GROUP_CUE_RE.search(text):
        # 「按客户分类分别有多少」：只提到一个分类词，但明确要求拆开看
        group_field = next(
            (name for name in spec.group_fields
             if catalog.field_label(spec, name)
             and catalog.field_label(spec, name) in text), '')
    if not group_field and not _COUNT_RE.search(text):
        return None

    keyword = ''
    keyword_field = ''
    if not group_field:
        keyword_field, keyword = db_value_match(spec, text, user)
        if not keyword:
            # 只提到一个状态词（"审理中的诉讼有多少条"）：当筛选条件用
            single = [(name, found[0]) for name, found in labels.items()
                      if len(found) == 1]
            if single:
                keyword_field, keyword = single[0]
    return {'spec': spec, 'keyword': keyword, 'keyword_field': keyword_field,
            'group_field': group_field, 'state': _state_of_text(spec, text)}


def stat_answer(stat, user, policy=None):
    """把统计意图算成人话：分组给每组条数 + 例子，计数给总数 + 主要分布。"""
    spec = stat['spec']
    keyword = stat.get('keyword') or ''
    group_field = stat.get('group_field') or ''
    state = stat.get('state')
    qs = base_queryset(spec, user)
    if keyword:
        # 值已确认归属某字段时用精确匹配："元/吨"不能把"美元/吨"也算进来
        qs = apply_filters(qs, spec, keyword=keyword,
                           keyword_field=stat.get('keyword_field') or '')
    if state is not None:
        qs = qs.filter(state.filter_q())
    total = qs.count()
    if not total:
        bits = []
        if keyword:
            bits.append(f'筛选：{keyword}')
        if state is not None:
            bits.append(f'状态：{state.label}')
        scope = f'（{"；".join(bits)}）' if bits else ''
        return f'「{spec.label}」没有符合条件的记录{scope}。', []
    if group_field:
        return _group_answer(spec, qs, total, group_field)
    return _count_answer(spec, qs, total, keyword, stat.get('keyword_field') or '',
                         state.label if state is not None else '')


def _samples(spec, qs, limit=3):
    objs = list(qs.order_by(resolve_order(spec, ''))[:limit])
    titles = [str(getattr(obj, spec.title_field, '') or f'#{obj.pk}')[:24] for obj in objs]
    return [t for t in titles if t], [catalog.build_ref(spec, obj) for obj in objs]


def _md_cell(value):
    """表格单元格：竖线要转义，换行压成空格，否则一行就把整张表拆坏。"""
    text = '' if value is None else str(value)
    return text.replace('|', '\\|').replace('\n', ' ').strip()


def _scope_note(keyword='', state_label=''):
    bits = []
    if keyword:
        bits.append(f'筛选：{keyword}')
    if state_label:
        bits.append(f'状态：{state_label}')
    return f"（{'；'.join(bits)}）" if bits else ''


def _group_answer(spec, qs, total, group_field):
    label = catalog.field_label(spec, group_field)
    unit = unit_of(spec)
    rows = list(qs.values(group_field).annotate(n=Count('id')).order_by('-n')[:10])
    lines = [f'「{spec.label}」按「{label}」分组，共 **{total} {unit}**：', '',
             f'| {label} | 数量 | 举例 |', '| --- | --- | --- |']
    refs = []
    for row in rows:
        value = row[group_field]
        name = catalog.label_for_value(spec, group_field, value)
        # 只举 2 条：面板 360px，三家公司的名字会把单元格挤成四五行的竖条
        titles, group_refs = _samples(spec, qs.filter(**{group_field: value}), limit=2)
        example = '、'.join(titles) if titles else '—'
        lines.append(f'| {_md_cell(name)} | {row["n"]} {unit} | {_md_cell(example)} |')
        for ref in group_refs:
            if len(refs) < 10:
                refs.append(ref)
    lines += ['', '> 每组只举例前几条，完整列表见下方引用卡片。']
    return '\n'.join(lines), refs


def _count_answer(spec, qs, total, keyword, keyword_field='', state_label=''):
    unit = unit_of(spec)
    if keyword:
        label = catalog.field_label(spec, keyword_field) if keyword_field else ''
        if label:
            head = f'「{spec.label}」中「{label}」为「{keyword}」的共 **{total} {unit}**'
        else:
            head = f'「{spec.label}」中，{keyword} 的记录共 **{total} {unit}**'
        if state_label:
            head += f'（状态：{state_label}）'
        head += '。'
    elif state_label:
        head = f'「{spec.label}」中{state_label}的共 **{total} {unit}**。'
    else:
        head = f'「{spec.label}」共 **{total} {unit}**。'
    lines = [head]
    # 顺带给一个主要分类的分布：用户问"一共多少"时，通常也想看构成
    for name in spec.group_fields:
        try:
            rows = list(qs.values(name).annotate(n=Count('id')).order_by('-n')[:8])
        except Exception:  # noqa: BLE001
            continue
        if 2 <= len(rows) <= 8:
            lines += ['', f'| {catalog.field_label(spec, name)} | 数量 |', '| --- | --- |']
            for row in rows:
                lines.append(
                    f'| {_md_cell(catalog.label_for_value(spec, name, row[name]))} '
                    f'| {row["n"]} {unit} |')
            break
    _titles, refs = _samples(spec, qs, limit=10)
    return '\n'.join(lines), refs


def _display_value(spec, key, value):
    """库里的代码值翻成人话："pending" → "待续/正常"，"fail" → "不通过"。

    只影响给人看的确定性回答；工具返回给模型的行仍是原始值，避免改变
    模型已经学会的取值（提示词里明确说过选项字段存的是代码）。
    """
    if not isinstance(value, str) or not value:
        return value
    for name, _choices in catalog.choice_fields(spec):
        if name == key:
            return catalog.label_for_value(spec, key, value)
    return value


_BORROW_MODULES = {'archive_borrow', 'license_borrow'}


def _row_status(spec, row):
    """借阅行的状态以"归还时间 + 应还日期"为准，不信落库的 status 字段。

    ``status`` 只在走页面借出/归还时刷新；历史数据里还写着"借出中"、
    实际早就逾期了。回答上方说"已逾期"、逐行却写"借出中"，用户只会觉得
    系统在胡说。
    """
    if spec.key not in _BORROW_MODULES:
        return None
    if row.get('return_time'):
        return '已归还'
    due = str(row.get('due_time') or '')[:10]
    if due:
        try:
            if date.fromisoformat(due) < timezone.localdate():
                return '已逾期'
        except ValueError:
            pass
    return '借出中'


def module_digest(spec, rows, total, columns, keyword='', state_label=''):
    """把某模块的列表结果说成人话（Markdown，前端按富文本渲染）。

    条目把**第一个字段当主语加粗**（编号/名称），其余字段用 ``·`` 连排；
    面板只有 360px 宽，这里刻意不用表格——6 列的表在窄屏里只会横向滚动。
    """
    unit = unit_of(spec)
    scope = _scope_note(keyword, state_label)
    if not total:
        return f'「{spec.label}」没有符合条件的记录{scope}。'
    shown = rows[:10]
    lines = [f'「{spec.label}」共 **{total} {unit}**{scope}，最近 {len(shown)} 条：', '']
    for row in shown:
        display = dict(row)
        status = _row_status(spec, row)
        if status and 'status' in display:
            display['status'] = status
        cells = [(columns.get(k, k), _display_value(spec, k, v))
                 for k, v in display.items()
                 if k != 'id' and v not in (None, '')]
        if not cells:
            continue
        _head_label, head_value = cells[0]
        rest = ' · '.join(f'{label} {value}' for label, value in cells[1:4])
        lines.append(f'- **{_md_cell(head_value)}**' + (f' · {rest}' if rest else ''))
    if total > len(shown):
        lines += ['', f'> 只列最近 {len(shown)} {unit}，完整列表见下方引用卡片或到列表页查看。']
    return '\n'.join(lines)


def local_lookup(user, question, policy=None, lead='', empty_lead=''):
    """按关键词在权限范围内本地检索（不产生数据外发）。

    ``lead`` 为结果前言；``empty_lead`` 为一无所获时的文案。两者留空时用
    "AI 暂时不可用"的口径（供 ``keyword_fallback`` 使用）。
    """
    policy = policy or security.policy()
    modules = registry.available_modules(user, True, policy)
    if not modules:
        return '', []

    candidates = keyword_candidates(question)
    hinted = hinted_modules(question, modules)
    recent_question = bool(_RECENT_RE.search(question or ''))
    hits, refs = [], []

    def add(spec, obj, note=''):
        detail = ''
        if spec.list_fields:
            row, _ = project(spec, obj, spec.list_fields[:2], policy, text_limit=40)
            detail = '，'.join(str(v) for v in row.values() if v)
        title = str(getattr(obj, spec.title_field, '') or f'#{obj.pk}')[:40]
        hits.append(f'- {spec.label}{note}：{title}' + (f'（{detail}）' if detail else ''))
        refs.append(catalog.build_ref(spec, obj))

    ordered = list(hinted) + [s for s in modules if s not in hinted]
    for spec in ordered:
        if len(refs) >= MAX_FALLBACK_REFS:
            break
        matched = []
        if spec.keyword_fields and candidates:
            for keyword in candidates:
                matched = list(
                    apply_filters(base_queryset(spec, user), spec, keyword=keyword)
                    .order_by(resolve_order(spec, ''))     # 命中多条时新的在前
                    [:PER_MODULE_HITS]
                )
                if matched:
                    break
        if matched:
            for obj in matched:
                add(spec, obj)
            continue
        # 点名了模块却没匹配上字段值：给最新的几条，让用户至少有个入口
        if spec in hinted:
            note = '（最新几条）' if recent_question else '（未匹配关键词，列出最新几条）'
            for obj in base_queryset(spec, user).order_by(resolve_order(spec, ''))[:PER_MODULE_HITS]:
                add(spec, obj, note)

    hits, refs = hits[:MAX_FALLBACK_REFS], refs[:MAX_FALLBACK_REFS]
    if not hits:
        if empty_lead:
            return empty_lead, []
        module_labels = '、'.join(s.label for s in modules[:8])
        return (
            'AI 暂时不可用，也没能用关键词在权限范围内匹配到记录。\n'
            f'你可以查询的模块有：{module_labels}。\n'
            '换个说法再问（带上客户名、单号或日期），或使用顶部全站搜索。'
        ), []

    answer = (lead or ('AI 暂时不可用，已按关键词在权限范围内检索，结果如下'
                       '（仅供参考，非 AI 回答）：')) + '\n' + '\n'.join(hits)
    return answer, refs


def keyword_fallback(user, question, policy=None):
    """模型不可用时的降级：关键词检索 + 点名模块的最近记录。

    这不是"更好的搜索"，而是"不让用户一无所获"。三级退让：
    1. 关键词命中（问句拆词后在各模块的关键字段里 icontains）；
    2. 问句点名的模块即使没有关键词命中，也列出它最新的几条（"最近发布了
       哪些制度"这类问题本来就没有可匹配的字段值）；
    3. 全都没有命中时，明确说明检索过哪些模块、建议怎么问，而不是一句
       "AI 不可用"就结束。

    注意：这是**本地查询**，不产生任何数据外发，因此不受云端外发策略限制；
    能看到哪些模块仍由 ``available_modules`` 按权限过滤。
    """
    return local_lookup(user, question, policy)

