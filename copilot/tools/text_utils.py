"""文本归一化：把"看起来不一样、其实指同一个东西"的字符串洗成同一个 slug。

典型场景（都是这个仓库里真实会遇到的）：

- 标点差异：``Test-Driven, Development.`` vs ``Test Driven Development``
- 全角/半角、大小写、首尾空格与不换行空格（U+00A0 / U+3000）
- 重音字母：``Katherine`` vs ``Katherine``（带组合音标）
- 拉丁文与中日韩之间的分隔号：``analytics:xe Go``、``ウィリアム・ハリソン``

刻意**不做**的两件事：

1. **不做 stemming / 词形还原**。那是检索层（Postgres FTS 或 Meilisearch）的活，
   在归一化里做词干会把 ``university`` 和 ``universities`` 提前压成一个词，
   后面想区分都来不及。
2. **不做跨语种别名映射**。同一位作者在人名 Voting 里可能出现
   ``Benjamin Franklin`` / ``富兰克林``，那是 ``alias_map`` 的职责，
   归一化阶段硬凑会让规则库变成一坨特例。

思路就一句话：**同一书写体系内做到一致，跨书写体系交给别名表。**
"""
from __future__ import annotations

import unicodedata

__all__ = ['deentitize', 'strip_accents', 'collapse_spaces', 'to_slug']

SPACES = {
    '\u00a0',   # 不换行空格（web 复制粘贴的头号元凶）
    '\u2007',   # 数字空格
    '\u202f',   # 窄不换行空格
    '\u3000',   # 全角空格
    '\t', '\n', '\r', '\f', '\v', ' ',
}
# HTML 里最常见的四个实体：其余交给调用方决定要不要 unescape
_ENTITIES = (
    ('&amp;', '&'), ('&quot;', '"'), ('&#39;', "'"), ('&apos;', "'"),
    ('&lt;', '<'), ('&gt;', '>'), ('&nbsp;', ' '),
)


# NFKD 拆不开的"排版变体" → ASCII 展开。范围刻意很窄：
# 只收**同一个字母的书写变体**（ß/ss、带斜杠的 Ł / Đ），它们本身的每日工作记录习惯
# 写法就是在没有这些字符的键盘上打成的等价形式。
#
# 反过来，Æ / Ø / Å / Þ 是**独立字母**（丹麦语、挪威语里的元音/辅音），不是给
# A/O 加的修饰符：把 Ærø 折成 Aero 会让两个不同实体撞名。这一层不去惹它们，
# 需要时请在 alias_map 里显式登记——那才是"这两个写法指同一个东西"的正确表达
# （§ReadRecord 的 alias_map 已登记 Ærø Andersen ↔ Aero Andersen 这种）。
_FOLD_MAP = str.maketrans({
    'ß': 'ss', 'ẞ': 'SS',
    'Ł': 'L', 'ł': 'l', 'Đ': 'D', 'đ': 'd',
})


def deentitize(value):
    """把常见 HTML 实体还原成字符（只处理最高频的几个）。"""
    text = '' if value is None else str(value)
    for entity, char in _ENTITIES:
        text = text.replace(entity, char)
    return text


def strip_accents(value):
    """去掉组合音标重音，但**保留**非拉丁字母本身。

    ``Æthelred`` → ``Aethelred`` 是对的；把 ``成`` 或 ``Ж`` 也"拉平"成
    某个拉丁字母就是胡来了——NFKD 不会那样做，这里也不会。
    """
    text = '' if value is None else str(value)
    decomposed = unicodedata.normalize('NFKD', text)
    stripped = ''.join(ch for ch in decomposed if not unicodedata.combining(ch))
    return unicodedata.normalize('NFKC', stripped)


def collapse_spaces(value):
    """各类空格统一为单个半角空格并去掉首尾空白。"""
    if value is None:
        return ''
    out, prev_space = [], False
    for ch in str(value):
        if ch in SPACES:
            if not prev_space:
                out.append(' ')
                prev_space = True
            continue
        out.append(ch)
        prev_space = False
    return ''.join(out).strip()


def to_slug(value, fallback=''):
    """归一化。

    规则刻意保守：**字母/数字/中日韩保留，其余符号一律当分隔符**。
    分隔而不是删除，是为了不让 ``analytics:xe Go`` 塌成 ``analyticsxego``，
    也不让 ``ウィリアム・ハリソン`` 粘成 ``ウィリアムハリソン``——
    连写会让后续的子串检索（IContains）直接失效。
    """
    if value is None:
        return fallback
    text = collapse_spaces(strip_accents(deentitize(str(value)).translate(_FOLD_MAP)))
    if not text:
        return fallback

    pieces, current = [], []
    for ch in text:
        if _is_kept(ch):
            current.append(ch)
        else:
            if current:
                pieces.append(''.join(current))
                current = []
    if current:
        pieces.append(''.join(current))
    if not pieces:
        # 全是符号的输入（例如 "-----"）：给它兜底值，别返回空串让调用方难堪
        return fallback
    return '-'.join(piece.lower() for piece in pieces)


def _is_kept(ch):
    """是否保留该字符：字母、数字（含中日韩以外的字母体系）都留。"""
    if ch.isalnum():
        return True
    # 组合记号、零宽字符（拷贝粘贴里常见的隐形污染）一律丢弃
    return unicodedata.category(ch) in ('Mn', 'Mc')
