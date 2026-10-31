"""会话槽位：让"那这个客户呢"能接上上一轮。

为什么不用"把更多历史塞给模型"：``MAX_HISTORY=4``、每条 600 字，看起来是
上下文不够，但真正的问题是**本地链路完全不看历史**——本地答得了的追问
（"那上个月的记录呢"）被当成全新问题，模块/关键词全丢，于是要么答错模块，
要么退回模型重新猜一遍。

做法是"结构化记忆 + 本地改写"：
1. 每轮把解析结果（模块/关键词/状态）写进 ``conversation.slots``；
2. 下一轮如果是明确追问（指代词、或以"那/还有/换成"开头），先把指代词换成
   槽位里的具体值，再走原有识别——不依赖模型猜。

**只在明确追问时启用**：换话题（问了一个新模块、没有指代词）时槽位照常
重写，避免"张冠李戴"——这是槽位记忆最常见的翻车方式。
"""

import re

from .. import catalog

# 指代词：出现即认为"这一问依赖上一轮"
PRONOUN_RE = re.compile(
    r'(这个|那个|这条|那条|这家|那家|该客户|该记录|上面(?:那个|这个|一条)?|'
    r'刚才(?:那个|这个|说的)?|他们|他的|她的|它)'
)
# 省略主语的追问："那上个月的记录呢""还有呢""换成已完成的呢"
FOLLOWUP_START_RE = re.compile(r'^(那|还有|再|然后|换成|按|如果|要是)')
FOLLOWUP_MAX_CHARS = 24
# 很短、又没点任何模块的问句（"赔了多少钱""有多少条"）：几乎一定是接着上一轮。
# 上一轮在聊客诉，这一句问的就是客诉的赔偿；当成新问题只会让模型重新猜模块。
FOLLOWUP_SHORT_CHARS = 12


def _names_module(question, modules):
    return any(_mentions(question, spec) for spec in modules)


def is_followup(question, slots, modules=()):
    if not slots:
        return False
    text = (question or '').strip()
    if not text:
        return False
    if PRONOUN_RE.search(text):
        return True
    if FOLLOWUP_START_RE.match(text) and len(text) <= FOLLOWUP_MAX_CHARS:
        return True
    if (slots.get('module') and modules and len(text) <= FOLLOWUP_SHORT_CHARS
            and not _names_module(text, modules)):
        return True
    return False


def _module_words(spec):
    words = [spec.label]
    try:
        from .planner import MODULE_ALIASES
        words.extend(MODULE_ALIASES.get(spec.key, ()))
    except Exception:  # noqa: BLE001 - 别名表取不到就只按 label 判断
        pass
    return [w for w in words if w and len(w) >= 2]


def _mentions(question, spec):
    return any(word in question for word in _module_words(spec))


def expand(question, slots, modules=()):
    """把追问改写成"带上下文"的问句。

    返回 ``(改写后的问题, 是否用了槽位)``。改写结果只用于识别与检索，
    展示给用户的仍是原问题。
    """
    question = question or ''
    if not is_followup(question, slots, modules):
        return question, False

    keyword = str(slots.get('keyword') or '').strip()
    module_key = str(slots.get('module') or '').strip()
    spec = catalog.get_module(module_key) if module_key else None

    text = question
    if keyword:
        text = PRONOUN_RE.sub(keyword, text)

    # 问题自己已经点了模块，就别再注入：否则"这个客户的保险合同呢"会被
    # 强行按上一轮的客诉模块解释，答非所问。
    already = bool(spec and _mentions(question, spec)) or any(
        _mentions(question, m) for m in modules if m.key != module_key)
    if spec and not already and spec.label not in text:
        text = f'{spec.label} {text}'
    if keyword and keyword not in text:
        text = f'{keyword} {text}'
    return ' '.join(text.split()), True


def remember(conversation, *, module='', keyword='', state='', field='', intent=''):
    """写入槽位；空值不覆盖已有值（追问常常只带模块，不该把关键词抹掉）。"""
    if conversation is None:
        return
    slots = dict(conversation.slots or {})
    # 换了模块但这一轮没抓到关键词：旧关键词必须清掉，否则下一句追问会把
    # 上一个话题的客户名带到新模块里（典型的"张冠李戴"）。
    if module and slots.get('module') and slots['module'] != module and not keyword:
        slots.pop('keyword', None)
    incoming = {
        'module': module, 'keyword': keyword, 'state': state,
        'field': field, 'intent': intent,
    }
    changed = False
    for key, value in incoming.items():
        value = str(value or '').strip()
        if value and slots.get(key) != value:
            slots[key] = value
            changed = True
    if not changed:
        return
    # 只保留最近一轮的结构，别让 slots 变成无界的历史堆栈
    conversation.slots = {k: v for k, v in slots.items() if v}
    conversation.save(update_fields=['slots'])
