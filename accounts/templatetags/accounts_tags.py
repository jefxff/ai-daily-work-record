"""前端权限判定标签 —— 菜单与按钮显隐的唯一入口。

用法：
    {% load accounts_tags %}
    {% can_view "samples" %}<a href="...">试样管理</a>{% endcan_view %}
    {% can "samples" "create" %}<a href="...">新增</a>{% endcan %}
    {% if request.user|can_view:"samples" %} ... {% endif %}
"""

from django import template

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
