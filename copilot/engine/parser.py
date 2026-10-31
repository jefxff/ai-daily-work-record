"""模型输出的容错解析。

本地小模型的输出经常夹带解释文字、代码围栏、尾逗号、单引号。这里按
"依次尝试、能解析就用"的顺序处理，最终兜底成普通文本回答——宁可
少调一次工具，也不要因为解析失败把整轮问答废掉。
"""

import json
import re

_FENCE_RE = re.compile(r'```(?:json)?\s*(.*?)```', re.S)
_TRAILING_COMMA_RE = re.compile(r',\s*([}\]])')

TOOL_KEYS = ('tool', 'name', 'action', 'function')  # 保留：说明哪些键会被识别为工具意图
# 模型可能用中文键：本地中文微调模型常输出 {"工具":"query_records","参数":{...}}
TOOL_NAME_KEYS = ('tool', 'action', 'function', '工具', '动作', '函数')
ARGS_KEYS = ('args', 'arguments', 'parameters', 'params', '参数')

# 模型想干写操作（删除/修改/导入）时的固定回复：不解释、不透传原始 JSON
UNSUPPORTED_REPLY = (
    '抱歉，我不能执行该操作。系统只开放查询类能力，'
    '删除或修改请到对应页面手动完成。'
)

# 工具名根本不存在时的回复。和上面分开：把"调错工具"说成"不支持写操作"
# 会让用户以为助手没有查询能力（2026-10-31：问铜价时模型调 metal_price，
# 用户看到的就是"只开放查询类能力…删除或修改"，完全对不上问题）。
UNKNOWN_TOOL_REPLY = (
    '抱歉，我没能找到「{name}」对应的查询入口。'
    '可以换个说法，比如「查一下某个模块的列表」，或直接到对应列表页用筛选/搜索查看。'
)

# 纯数据 JSON 里常见的协议键：出现这些说明是"工具调用/答案包装"，不是记录
PROTOCOL_KEYS = frozenset({
    'tool', 'action', 'function', 'args', 'arguments', 'parameters', 'params',
    'answer', 'content', 'reply', 'final', 'error', 'hint', 'ok',
    '工具', '动作', '函数', '参数', '答案', '内容',
})


def _repair(text):
    text = _TRAILING_COMMA_RE.sub(r'\1', text)
    # 仅替换出现在键/值边界的 ASCII 单引号，避免动到中文正文
    if '"' not in text and "'" in text:
        text = text.replace("'", '"')
    return text


def extract_json(text):
    """从任意文本中抽出第一个可解析的 JSON 对象；失败返回 None。"""
    if not text:
        return None
    candidates = []
    fence = _FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1).strip())
    start, end = text.find('{'), text.rfind('}')
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    candidates.append(text.strip())

    for raw in candidates:
        for candidate in (raw, _repair(raw)):
            try:
                value = json.loads(candidate)
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict):
                return value
    return None


def _as_args(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        return parsed if isinstance(parsed, dict) else {}
    return {}


def parse_decision(text, tool_names):
    """把模型输出解析成决策。

    返回四种之一：
    - ``{'type': 'tool', 'name', 'args'}``        调用已注册工具
    - ``{'type': 'unsupported', 'name'}``         模型想调不存在的工具（含删除类）
    - ``{'type': 'final', 'content'}``            直接回答
    """
    obj = extract_json(text)
    if obj:
        name, explicit = '', False
        for key in ('tool', 'action', 'function', '工具', '动作', '函数'):
            if isinstance(obj.get(key), str) and obj[key].strip():
                name, explicit = obj[key].strip(), True
                break
        # 单独的 name 字段常见于业务 JSON（如 {"name":"张三"}），
        # 只有同时带参数键时才认定为工具调用意图，避免误判。
        if not explicit and isinstance(obj.get('name'), str) and obj['name'].strip():
            if 'args' in obj or 'arguments' in obj:
                name, explicit = obj['name'].strip(), True

        if explicit:
            if name in tool_names:
                args = {}
                for key in ARGS_KEYS:
                    if key in obj:
                        args = _as_args(obj[key])
                        break
                return {'type': 'tool', 'name': name, 'args': args}
            return {'type': 'unsupported', 'name': name}

        for key in ('answer', 'content', 'reply', 'final'):
            if isinstance(obj.get(key), str) and obj[key].strip():
                return {'type': 'final', 'content': obj[key].strip()}

    return {'type': 'final', 'content': (text or '').strip()}


def as_json_object(text):
    """整段就是 JSON 对象时返回该 dict，否则 None（用于识别"JSON 当答案"）。"""
    stripped = (text or '').strip()
    if not (stripped.startswith('{') and stripped.endswith('}')):
        return None
    obj = extract_json(stripped)
    return obj if isinstance(obj, dict) and obj else None


def has_protocol_keys(obj):
    """这个 dict 是不是"工具调用/答案包装"，而不是一条业务记录。"""
    return bool(PROTOCOL_KEYS & {str(k).lower() for k in (obj or {})})


# 模型偶尔把整段回答塞进一对大括号里（2026-11-01 实测：本地 7B 对
# 「月结60天的客户有哪些」输出 ``{ "总共有 0 家…" }``，面板上就带着花括号
# 显示）。大括号在中文回答里没有语义，只有 JSON 才需要——不是 JSON 就剥掉。
_BRACE_ONLY_RE = re.compile(r'[{}]')
_BRACED_QUOTE_RE = re.compile(r'(?m)^\s*"\s*(.+?)\s*"\s*$')

# 退化检测：同一句反复出现说明模型在原地打转（实测出现过 40 多次
# "无其他记录。无其他信息。无其他数据。"）。这不是"回答很长"，是坏掉了。
_REPEAT_MIN_PARTS = 10
_REPEAT_MAX_RATIO = 0.5


def _strip_stray_braces(text):
    """剥掉没有语义的花括号；带 JSON 键值痕迹的不动，交给 as_json_object。"""
    if '{' not in text and '}' not in text:
        return text
    if re.search(r'"\s*:', text):
        return text
    cleaned = _BRACE_ONLY_RE.sub('', text)
    # 剥完常留下包住整行的裸引号（"{ "总共有…" }"），一并去掉
    cleaned = _BRACED_QUOTE_RE.sub(r'\1', cleaned)
    return cleaned.strip()


def _split_sentences(text):
    return [p for p in re.split(r'(?<=[。！？!?\n])', text) if p.strip()]


def is_degenerate(text):
    """这段回答是不是"原地打转"：重复句占了多数。

    7B 在拿到"0 条"之后会一路写下去（实测 40+ 句"无其他记录。无其他信息。"）。
    这种内容既不是结论也不可读，上层应改用本地兜底文案，而不是把它截一半给用户。
    """
    parts = _split_sentences(text or '')
    if len(parts) < _REPEAT_MIN_PARTS:
        return False
    return len({p.strip() for p in parts}) / len(parts) <= _REPEAT_MAX_RATIO


def _collapse_repeats(text):
    """掐掉原地打转的重复句子，只保留每个不同句子的首次出现。"""
    parts = _split_sentences(text)
    if len(parts) < _REPEAT_MIN_PARTS:
        return text
    unique = {p.strip() for p in parts}
    if len(unique) / len(parts) > _REPEAT_MAX_RATIO:
        return text
    kept, seen = [], set()
    for part in parts:
        key = part.strip()
        if key in seen:
            continue
        seen.add(key)
        kept.append(part)
    return ''.join(kept).rstrip() + '\n\n（这段说明在重复，已截断。）'


def _strip_lone_quotes(text):
    """只有左半边引号的回答：那对引号本来是用来"包裹 JSON"的，剥掉不影响语义。"""
    for quote in ('"', '“', '”'):
        if text.startswith(quote) and text.count(quote) == 1:
            return text[1:].lstrip()
    return text


def tidy_answer(text):
    """模型输出的两道整容：剥掉没有语义的花括号、掐掉打转的重复。"""
    if not text:
        return text
    return _strip_lone_quotes(_collapse_repeats(_strip_stray_braces(text)))


def as_record_object(text):
    """判断模型输出是不是"直接把一条记录当答案"（返回该 dict，否则 None）。

    本地小模型查完数据后常回一段 ``{"客户名称":"…","业务员":"…"}``：它不是
    工具调用、也没有 answer 键，会被当成最终答案原样显示。要识别的正是这种，
    但不能误伤正常回答——所以要求整段就是 JSON（首尾 {}），且不含协议键。
    """
    obj = as_json_object(text)
    if obj is None or has_protocol_keys(obj):
        return None
    return obj
