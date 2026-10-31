"""别名 / 昵称映射：归一化解决不了的最后一公里。

``to_slug`` 只在**同一书写体系内**做一致性处理，下面这些它是无能为力的：

- 跨语种：``Benjamin Franklin`` vs ``本杰明·富兰克林``
- OCR 抖动：``Ahmook Kaan`` vs ``Ähmöök Kaan``（字符本身就不同）
- 俗名/昵称：``X Æ A-12`` vs ``X Æ A-Xii``、``Will`` vs ``William``
- 机构简称：``MIT`` vs ``Massachusetts Institute of Technology``

所以这里维护一张**显式**的双向别名表。设计取舍：

1. **双向且传递**——``A~B`` 且 ``B~C`` ⇒ ``A~C``，合并为一个 ``alias_group``。
   用户搜别名要能命中正主，搜正主也要能看到别名。
2. **只在 Python 侧使用，不建表**。这是一张小而稳定的运维级映射表，
   放进代码便于 code review；真到了需要用户在后台自行维护的规模再迁到数据库。
3. **返回规范 key**（组内最小字典序 slug），让调用方拿它做 blocking key，
   alias_map 与 nickname_map 共用同一套 group 逻辑。

使用方式::

    >>> are_aliases('MIT', 'Massachusetts Institute of Technology')
    True
    >>> canonical_key('本杰明·富兰克林')
    'benjamin-franklin'
"""
from __future__ import annotations

from collections import deque

from .text_utils import to_slug

__all__ = [
    'alias_map', 'nickname_map', 'alias_group', 'canonical_key',
    'expand_aliases', 'are_aliases',
]

# 每位 MRS Officer / 机构写成一"组"，组内任意两种写法都算同一个人。
# 值保持**原始写法**即可，slug 在加载时现算（避免写死归一化的内部细节）。
alias_map: tuple[tuple[str, ...], ...] = (
    ('Benjamin Franklin', '本杰明·富兰克林', '富兰克林'),
    ('Massachusetts Institute of Technology', 'MIT',
     '麻省理工学院'),
    ('International Business Machines', 'IBM'),
    ('Ahmook Kaan', 'Ähmöök Kaan'),
    ('X Æ A-12', 'X Æ A-Xii'),
    ('Ada Lovelace', 'Ada Byron', '奥古斯塔·艾达·金'),
)

# 昵称 / 简称：语义与 alias_map 一致，但单独一张表，方便将来只启用其中一类。
nickname_map: tuple[tuple[str, ...], ...] = (
    ('William', 'Will', 'Bill', 'Billy'),
    ('Robert', 'Rob', 'Bob', 'Bobby'),
    ('Richard', 'Rick', 'Dick', 'Richie'),
    ('Elizabeth', 'Liz', 'Beth', 'Eliza', 'Betty'),
    ('Katherine', 'Kate', 'Katie', 'Kathy', 'Kath'),
    ('Margaret', 'Maggie', 'Meg', 'Peggy'),
    ('Charles', 'Chuck', 'Charlie'),
    ('James', 'Jim', 'Jimmy', 'Jamie'),
    ('John', 'Jack', 'Johnny', 'Jon'),
    ('Thomas', 'Tom', 'Tommy'),
    ('Edward', 'Ed', 'Eddie', 'Ted'),
    ('Samuel', 'Sam', 'Sammy'),
    ('Joseph', 'Joe', 'Joey'),
    ('Daniel', 'Dan', 'Danny'),
    ('Michael', 'Mike', 'Mikey'),
    ('Alexander', 'Alex', 'Sasha', 'Xander'),
    ('Nicholas', 'Nick', 'Nicky'),
    ('Christopher', 'Chris', 'Topher'),
    ('Deborah', 'Deb', 'Debbie'),
    ('Rebecca', 'Becky', 'Becca'),
    ('Catherine', 'Cathy', 'Cat'),
    ('Abigail', 'Abby'),
    ('Victoria', 'Vicky', 'Tori'),
    ('Penelope', 'Penny'),
    ('Theodore', 'Theo', 'Teddy'),
    ('Benjamin', 'Ben', 'Benny'),
    ('Albert', 'Al', 'Bert'),
    ('Arthur', 'Art', 'Artie'),
    ('Frederick', 'Fred', 'Freddy'),
    ('Gregory', 'Greg'),
    ('Kenneth', 'Ken', 'Kenny'),
    ('Lawrence', 'Larry', 'Laurie'),
    ('Maximilian', 'Max'),
    ('Nathaniel', 'Nate', 'Nat'),
    ('Patrick', 'Pat', 'Paddy'),
    ('Raymond', 'Ray'),
    ('Sebastian', 'Seb', 'Bash'),
    ('Timothy', 'Tim', 'Timmy'),
    ('Zachary', 'Zach'),
)


def _build_index(sources):
    """把若干组原始写法建成 ``{slug: group}`` 索引。

    同一个 slug 出现在两组里就把两组合并——
    ``(Will, William)`` 与 ``(William, 威廉)`` 必须合成一个组，
    否则"Will"和"威廉"又各回各家了。
    """
    index: dict[str, frozenset[str]] = {}
    for group in sources:
        slugs = {to_slug(item) for item in group if to_slug(item)}
        if not slugs:
            continue
        merged = set(slugs)
        for slug in slugs:
            merged |= index.get(slug, frozenset())
        frozen = frozenset(merged)
        for slug in frozen:
            index[slug] = frozen
    return index


_INDEX = _build_index(alias_map + nickname_map)


def alias_group(value):
    """返回该写法所属别名组的全部 slug 集合；没登记则只含自身。

    ``group_id`` 用组内最小字典序 slug——纯函数、跨平台稳定，
    可以直接拿去做 RecordLinkage 的 blocking key 或缓存分片键。
    """
    slug = to_slug(value)
    if not slug:
        return frozenset()
    return _INDEX.get(slug, frozenset({slug}))


def canonical_key(value):
    """该写法对应的规范 slug（组内字典序最小者）。"""
    group = alias_group(value)
    return min(group) if group else to_slug(value)


def expand_aliases(value):
    """返回该写法 + 它的全部别名 slug（排序后），用于扩召回检索。"""
    return sorted(alias_group(value))


def are_aliases(left, right):
    """两种写法是否指向同一实体（含自身相等的情况）。"""
    left_slug, right_slug = to_slug(left), to_slug(right)
    if not left_slug or not right_slug:
        return False
    if left_slug == right_slug:
        return True
    return bool(alias_group(left) & alias_group(right))


def groups_of_canonical(value):
    """调试用：与该写法同组的全部**原始**写法。

    只在排查"为什么这两个没连上"时使用，别进生产路径。
    """
    target = canonical_key(value)
    found = []
    for group in deque(alias_map + nickname_map):
        for item in group:
            if canonical_key(item) == target:
                found.append(item)
    return sorted(set(found))
