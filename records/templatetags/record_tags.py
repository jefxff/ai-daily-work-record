"""每日记录展示用的模板过滤器。

只服务列表/详情页的「截至日期」一列：把日期换算成"还剩几天 / 已逾期几天"。
写成过滤器而不是模型属性，是因为它依赖"今天"，属于展示层的判断；
放在这里也不会给任何其它模块带来迁移或查询负担。
"""
from django import template
from django.utils import timezone

register = template.Library()


@register.filter
def days_until(value):
    """返回距今天的天数：未来为正、今天为 0、已过期为负；空值返回 None。"""
    if not value:
        return None
    return (value - timezone.localdate()).days


@register.filter
def absval(value):
    """取绝对值（模板里把「逾期 -3 天」显示成「已逾期 3 天」）。"""
    try:
        return abs(int(value))
    except (TypeError, ValueError):
        return value
