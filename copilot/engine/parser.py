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
