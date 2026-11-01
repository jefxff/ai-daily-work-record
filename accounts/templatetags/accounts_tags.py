"""前端权限判定标签 —— 菜单与按钮显隐的唯一入口。

用法：
    {% load accounts_tags %}
    {% can_view "samples" %}<a href="...">试样管理</a>{% endcan_view %}
    {% can "samples" "create" %}<a href="...">新增</a>{% endcan %}
    {% if request.user|can_view:"samples" %} ... {% endif %}
"""

from django import template
from django.utils.safestring import mark_safe

from accounts.services import (
    can_view_module,
    get_display_name,
    get_user_roles_display,
    has_perm,
)

register = template.Library()


class _CheckNode(template.Node):
    def __init__(self, nodelist, args, check_fn):
        self.nodelist = nodelist
        self.args = [template.Variable(a) for a in args]
        self.check_fn = check_fn

    def render(self, context):
        try:
            values = [a.resolve(context) for a in self.args]
        except template.VariableDoesNotExist:
            return ""
        user = context["request"].user if "request" in context else None
        if self.check_fn(user, *values):
            return self.nodelist.render(context)
        return ""


@register.tag("can_view")
def do_can_view(parser, token):
    try:
        _tag, module = token.split_contents()
    except ValueError:
        raise template.TemplateSyntaxError("can_view 用法：{% can_view \"module\" %}")
    nodelist = parser.parse(("endcan_view",))
    parser.delete_first_token()
    return _CheckNode(nodelist, [module], lambda user, module: can_view_module(user, module))


@register.tag("can")
def do_can(parser, token):
    try:
        _tag, module, action = token.split_contents()
    except ValueError:
        raise template.TemplateSyntaxError("can 用法：{% can \"module\" \"action\" %}")
    nodelist = parser.parse(("endcan",))
    parser.delete_first_token()
    return _CheckNode(
        nodelist, [module, action], lambda user, module, action: has_perm(user, module, action)
    )


@register.tag("can_view_any")
def do_can_view_any(parser, token):
    """{% can_view_any "m1" "m2" %}...{% endcan_view_any %}：任一模块可见即渲染。"""
    bits = token.split_contents()[1:]
    if not bits:
        raise template.TemplateSyntaxError("can_view_any 至少需要一个模块参数")
    nodelist = parser.parse(("endcan_view_any",))
    parser.delete_first_token()

    def check(user, *modules):
        return any(can_view_module(user, m) for m in modules)

    return _CheckNode(nodelist, bits, check)


@register.filter(name="can_view")
def filter_can_view(user, module):
    return can_view_module(user, module)


@register.filter(name="can_do")
def filter_can_do(user, args):
    """{% if request.user|can_do:'samples.create' %}"""
    module, _, action = str(args).partition(".")
    return has_perm(user, module, action)


@register.filter(name="visible_count")
def filter_visible_count(user):
    from accounts.services import visible_modules
    try:
        return len(visible_modules(user))
    except Exception:
        return 0


@register.filter(name="display_name")
def filter_display_name(user):
    return get_display_name(user)


@register.filter(name="roles_text")
def filter_roles_text(user):
    roles = get_user_roles_display(user)
    return "、".join(roles) if roles else "未分配角色"


# ---------------------------------------------------------------------------
# 当前页高亮
# ---------------------------------------------------------------------------
# 侧栏与面包屑共用「URL 路径前缀」判定（records/nav.py 是路径与模块的映射表）。
# 原先模板里写的是 `{% if request.path == '/workspace/' %}`，全等比较让详情页
# 永远匹配不上；改成前缀匹配后，/daily_record/detail/12/ 也会高亮「每日记录」。
# 前缀以 '=' 开头表示精确相等，用于「试样看板」与「样品台账」这类同前缀兄弟页。

def _nav_hit(request, prefix):
    path = getattr(request, 'path', '') or ''
    if prefix.startswith('='):
        return path == prefix[1:]
    return path.startswith(prefix)


@register.simple_tag(takes_context=True)
def nav_class(context, *prefixes):
    """命中则输出 `class="active" aria-current="page"`，否则输出空串。

    用法：<a href="{% url 'daily_record_list' %}" {% nav_class '/daily_record/' %}>
    """
    request = context.get('request')
    if not request or not prefixes:
        return ''
    if any(_nav_hit(request, p) for p in prefixes):
        return mark_safe('class="active" aria-current="page"')
    return ''


@register.simple_tag(takes_context=True)
def nav_active(context, *prefixes):
    """布尔版本，供需要拼接 class 或 aria-expanded 的场景使用。

    用法：{% nav_active '/samples/' as cur %}
          <button class="dropdown-toggle{% if cur %} active{% endif %}"
                  aria-expanded="{% if cur %}true{% else %}false{% endif %}">
    """
    request = context.get('request')
    if not request or not prefixes:
        return False
    return any(_nav_hit(request, p) for p in prefixes)


@register.simple_tag(takes_context=True)
def has_filters(context):
    """当前 URL 是否带着「真正的筛选条件」（排除翻页参数）。

    列表页的 chips 容器一直存在，但只有真正筛过才需要「全部清除」入口；
    ?page=2 这类纯翻页不算筛选，否则清除链接会显得莫名其妙。
    """
    request = context.get('request')
    if not request:
        return False
    for key, value in request.GET.items():
        if key in ('page', 'per_page'):
            continue
        if value:
            return True
    return False
