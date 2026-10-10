"""试样管理自定义模板标签。"""

from django import template

register = template.Library()


@register.filter
def split(value, sep=","):
    """按分隔符拆分字符串为列表。"""
    return str(value).split(sep)
