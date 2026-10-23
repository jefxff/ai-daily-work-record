"""HTTP 请求处理公共工具（复核报告 CODE-01：消灭各模块复制粘贴的重复实现）。

此前 `_is_ajax` 有 6 份、`_per_page` 4 份、`_parse_date` 7 份，行为略有漂移。
这里给出单一实现，各模块按需导入；函数签名保持与原实现一致，替换零行为变化。
"""

from datetime import date, datetime

_TRUE_STRINGS = {"1", "true", "yes", "on"}


def is_ajax(request) -> bool:
    """是否为 AJAX 请求（前端以 X-Requested-With 标识）。"""
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def per_page(request, default: int = 10, *, choices=None, minimum: int = 5, maximum: int = 100) -> int:
    """从 GET 参数 per_page 取每页条数。

    - 传入 `choices` 时按白名单取值（列表页 10/30/50），非法值回落 default；
    - 不传 `choices` 时夹在 [minimum, maximum]（通知中心 5~100）。
    """
    try:
        value = int(request.GET.get("per_page", default))
    except (TypeError, ValueError):
        value = default
    if choices:
        return value if value in choices else default
    return min(max(value, minimum), maximum)


def parse_date(value, default=None):
    """严格解析 `YYYY-MM-DD`（允许带时间部分的参数只取日期段）。

    空值、非法值统一返回 default（原各模块实现即如此：有的返回 None，
    有的返回调用方给的默认值）。
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not raw:
        return default
    if " " in raw:
        raw = raw.split(" ", 1)[0]
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return default
