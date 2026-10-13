"""分页辅助 template filter：页码窗口化，避免页数多时全量展开挤爆布局。"""

from django import template

register = template.Library()


@register.filter
def page_window(page_obj, span=2):
    """返回页码窗口列表：当前页 ±span 外加首尾页，None 表示省略号（…）。

    用法：
        {% for p in page_obj|page_window %}
            {% if p %}页码按钮{% else %}…{% endif %}
        {% endfor %}
    """
    try:
        span = int(span)
    except (TypeError, ValueError):
        span = 2
    num = page_obj.number
    total = page_obj.paginator.num_pages
    pages = {1, total, num}
    for i in range(num - span, num + span + 1):
        if 1 <= i <= total:
            pages.add(i)
    out = []
    prev = 0
    for p in sorted(pages):
        if p - prev > 1:
            out.append(None)
        out.append(p)
        prev = p
    return out
