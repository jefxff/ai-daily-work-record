"""HTML 文本处理的小工具。"""


def demote_headings(html, by=1):
    """把 HTML 里的标题整体降级，保持相对层级不变。

    Markdown 正文里写 `# 标题` 会生成 <h1>，而页面自己已经有一个 h1，
    一页出现两个一级标题（B7 要求每页恰有一个 h1）。把它降一级即可：
    <h1>→<h2>、<h2>→<h3>……<h6> 不再下移。

    必须从最深的一层往上替换，否则 h1→h2 的结果会被下一轮 h2→h3 再降一次。
    """
    if not html:
        return html
    for level in range(6 - by, 0, -1):
        html = html.replace(f"<h{level}>", f"<h{level + by}>")
        html = html.replace(f"<h{level} ", f"<h{level + by} ")
        html = html.replace(f"</h{level}>", f"</h{level + by}>")
    return html
