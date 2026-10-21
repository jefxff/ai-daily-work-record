"""Markdown ↔ 大纲树 双向转换（幕布化 P0，纯函数零依赖）。

解析规则（与方案 03 第六节对齐）：
- ``- `` / ``* `` / ``1.`` 加缩进（2/4 空格或 Tab）→ 层级，缩进单位自动探测
- ``- [ ]`` / ``- [x]`` → todo + is_done
- ``#`` ~ ``###`` → heading（heading_level 1-3，更深的标题按 3 处理）
- ``> `` → quote（每行一个节点）
- `````` ``` `````` 围栏 → code（整块单节点，正文存 note，info string 存 text）
- ``---`` / ``***`` 分隔线 → divider
- 其余普通行 → text 节点（每行一个，绝不丢内容）

反向 tree_to_md() 统一输出 2 空格缩进的嵌套列表，保证
md → tree → md 幂等（第二轮起逐字节稳定，tests_outline 守住）。
"""

import re

__all__ = ['md_to_tree', 'tree_to_md', 'count_nodes', 'depth_of', 'new_node',
           'split_todo_due']

_BULLET_RE = re.compile(r'^(\s*)(?:[-*]|\d+[.)])\s+(.*)$')
_TODO_RE = re.compile(r'^\[( |x|X)\]\s+(.*)$')
_HEADING_RE = re.compile(r'^(\s*)(#{1,6})\s+(.*)$')
_QUOTE_RE = re.compile(r'^(\s*)>\s?(.*)$')
_FENCE_RE = re.compile(r'^(\s*)```(.*)$')
_DIVIDER_RE = re.compile(r'^\s*(?:-{3,}|\*{3,}|_{3,})\s*$')
# 待办转写：行首 ``YYYY-MM-DD`` 前缀 → due_date（服务端保存与前端编辑器共用同一规则）
_DATE_PREFIX_RE = re.compile(r'^(\d{4}-\d{2}-\d{2})(?:\s+(.*))?$')


def split_todo_due(text):
    """待办文本 → (due_str|None, 剩余文本)。

    ``2026-10-25 交报告`` → ``('2026-10-25', '交报告')``；
    无日期形状前缀 → ``(None, 原文)``（日期合法性由调用方再验，这里只认形状）。
    """
    m = _DATE_PREFIX_RE.match((text or '').strip())
    if m:
        return m.group(1), (m.group(2) or '').strip()
    return None, (text or '').strip()


def new_node(text='', node_type='text', **kw):
    """构造一个树节点 dict（树在内存/接口层统一用 dict 表示）。"""
    node = {
        'uid': kw.get('uid') or '',
        'type': node_type,
        'text': text or '',
        'done': bool(kw.get('done', False)),
        'note': kw.get('note') or '',
        'heading_level': kw.get('heading_level'),
        'collapsed': bool(kw.get('collapsed', False)),
        'children': [],
    }
    return node


def _indent_width(raw_indent):
    """缩进宽度：Tab 记 1 个单位，空格按字符数。"""
    return raw_indent.replace('\t', ' ' * 1).count(' ') + raw_indent.count('\t')


def _detect_unit(lines):
    """探测缩进单位：取列表行最小正缩进（2/4 空格常见，Tab 按 1 计）。"""
    widths = []
    in_fence = False
    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _BULLET_RE.match(line)
        if m:
            w = _indent_width(m.group(1))
            if w > 0:
                widths.append(w)
    return min(widths) if widths else None


def md_to_tree(content):
    """Markdown 文本 → 树（list[dict]）。坏输入返回空树，绝不抛异常。"""
    try:
        lines = (content or '').replace('\r\n', '\n').replace('\r', '\n').split('\n')
        roots, stack = [], []   # stack: [(level, node)]
        unit = _detect_unit(lines) or 2
        in_fence = False
        fence_info, fence_indent, fence_lines = '', 0, []

        def _attach(level, node):
            while stack and stack[-1][0] >= level:
                stack.pop()
            if stack:
                stack[-1][1]['children'].append(node)
            else:
                roots.append(node)
            stack.append((level, node))

        for raw in lines:
            if in_fence:
                if _FENCE_RE.match(raw):
                    node = new_node(fence_info, 'code', note='\n'.join(fence_lines))
                    _attach(_indent_width(fence_indent) // unit, node)
                    in_fence, fence_info, fence_lines = False, '', []
                else:
                    fence_lines.append(raw)
                continue

            fence = _FENCE_RE.match(raw)
            if fence:
                in_fence, fence_info, fence_indent = True, fence.group(2).strip(), fence.group(1)
                fence_lines = []
                continue

            if not raw.strip():
                # 空行不重置栈：松散列表（条目间有空行）靠缩进级别挂载即可
                continue

            heading = _HEADING_RE.match(raw)
            if heading:
                level = min(len(heading.group(2)), 3)
                node = new_node(heading.group(3).strip(), 'heading',
                                heading_level=level)
                _attach(_indent_width(heading.group(1)) // unit, node)
                continue

            divider = _DIVIDER_RE.match(raw)
            if divider:
                _attach(0, new_node('', 'divider'))
                continue

            quote = _QUOTE_RE.match(raw)
            if quote:
                node = new_node(quote.group(2).strip(), 'quote')
                _attach(_indent_width(quote.group(1)) // unit, node)
                continue

            bullet = _BULLET_RE.match(raw)
            if bullet:
                text = bullet.group(2).strip()
                todo = _TODO_RE.match(text)
                if todo:
                    node = new_node(todo.group(2).strip(), 'todo',
                                    done=(todo.group(1).lower() == 'x'))
                else:
                    node = new_node(text, 'text')
                _attach(_indent_width(bullet.group(1)) // unit, node)
                continue

            # 普通行：缩进决定挂载层级，每行一个 text 节点（不丢内容）
            level = _indent_width(raw[:len(raw) - len(raw.lstrip())]) // unit
            _attach(level, new_node(raw.strip(), 'text'))

        return roots
    except Exception:
        return []


def _render(node, level, out):
    indent = '  ' * level
    ntype = node.get('type') or 'text'
    text = (node.get('text') or '').replace('\n', ' ').strip()
    if ntype == 'heading':
        lv = node.get('heading_level') or 1
        out.append(f'{indent}{"#" * lv} {text}'.rstrip())
    elif ntype == 'todo':
        mark = '[x]' if node.get('done') else '[ ]'
        # 到期日写回文本前缀（``YYYY-MM-DD 事项``）：content 兼容层不丢截止日，
        # 且 md → tree → md 保持逐字节幂等（编辑器 GET 时服务端再剥离成 due 字段）。
        if node.get('due') and not _DATE_PREFIX_RE.match(text):
            text = f"{node['due']} {text}".strip()
        out.append(f'{indent}- {mark} {text}'.rstrip())
    elif ntype == 'quote':
        out.append(f'{indent}> {text}'.rstrip())
    elif ntype == 'divider':
        out.append(f'{indent}---')
    elif ntype == 'code':
        out.append(f'{indent}```{text}'.rstrip())
        if node.get('note'):
            out.append(node['note'])
        out.append(f'{indent}```')
    else:
        out.append(f'{indent}- {text}'.rstrip())
    for child in node.get('children') or []:
        _render(child, level + 1, out)


def tree_to_md(tree):
    """树 → Markdown（统一 2 空格缩进嵌套列表；round-trip 幂等）。"""
    out = []
    for node in tree or []:
        _render(node, 0, out)
    return '\n'.join(out)


def count_nodes(tree):
    return sum(1 + count_nodes(n.get('children') or []) for n in tree or [])


def depth_of(tree):
    if not tree:
        return 0
    return 1 + max(depth_of(n.get('children') or []) for n in tree)
