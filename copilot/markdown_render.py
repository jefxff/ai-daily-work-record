"""回答正文的 Markdown → 安全 HTML。

回答有两个来源，安全性完全不同：

1. **确定性模板**（``planner.module_digest`` 等）：字符串是我们自己拼的，
   但仍可能夹带库里的脏数据（客户名里写 ``<img onerror=...>``）；
2. **模型输出**：完全不可信，可能带回 ``<script>``、``javascript:`` 链接。

所以一律走 ``markdown`` 渲染 + ``bleach`` 白名单净化，前端只负责
``innerHTML``。白名单刻意不含 ``img``/``iframe``/``style``：聊天回答里
不需要外部图片，少了这条口子就少一类"用图片探测内网"的风险。
"""

import re

import bleach
import markdown

ALLOWED_TAGS = [
    'p', 'br', 'hr', 'strong', 'em', 'del',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'ul', 'ol', 'li', 'blockquote', 'code', 'pre',
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
    'a', 'span',
]
ALLOWED_ATTRS = {
    'a': ['href', 'title', 'target', 'rel'],
    'th': ['align'],
    'td': ['align'],
    'span': ['class'],
    'code': ['class'],
    'pre': ['class'],
}
PROTOCOLS = ['http', 'https', 'mailto']

# target=_blank 必须带 rel，否则新窗口能通过 window.opener 反向控制父页面
_HAS_REL_RE = re.compile(r'(<a\b[^>]*\btarget="_blank"[^>]*?)\brel="[^"]*"', re.I)
_NO_REL_RE = re.compile(r'(<a\b(?![^>]*\brel=)[^>]*\btarget="_blank")', re.I)

EXTENSIONS = [
    'markdown.extensions.extra',      # 表格、代码块、属性列表
    'markdown.extensions.sane_lists',  # 中文语境下的列表分隔更稳
    'markdown.extensions.nl2br',       # 聊天场景：换行就是换行
]


def render(text):
    """Markdown 正文 → 可安全塞进 innerHTML 的 HTML。"""
    if not text:
        return ''
    html = markdown.markdown(str(text), extensions=EXTENSIONS, output_format='html')
    cleaned = bleach.clean(
        html, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS,
        protocols=PROTOCOLS, strip=True,
    )
    cleaned = _HAS_REL_RE.sub(r'\1 rel="noopener noreferrer"', cleaned)
    cleaned = _NO_REL_RE.sub(r'\1 rel="noopener noreferrer"', cleaned)
    return cleaned


def to_plain(text):
    """把 markdown 压成纯文本（复制/导出/兜底用）。

    表格的单元格之间用制表符分隔、行之间换行，粘到 Excel/邮件里仍然竖着对齐。
    """
    if not text:
        return ''
    html = render(text)
    html = re.sub(r'</t[dh]>\s*(?=<t[dh])', '\t', html)
    html = re.sub(r'</(?:p|li|tr|h[1-6]|blockquote|pre|div)>|<br\s*/?>', '\n', html, flags=re.I)
    plain = re.sub(r'<[^>]+>', '', html)
    plain = (plain.replace('&amp;', '&').replace('&lt;', '<')
             .replace('&gt;', '>').replace('&quot;', '"').replace('&#39;', "'")
             .replace('&nbsp;', ' '))
    plain = re.sub(r'[ \t]+\n', '\n', plain)
    return re.sub(r'\n{3,}', '\n\n', plain).strip()
