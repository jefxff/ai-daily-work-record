"""DRF 统一异常响应结构（审计 X-05）。

响应同时保留 `detail` / `error` 以及原始字段键，确保现有前端读取方式不被破坏。
结构：
    {"code": <HTTP 状态码>, "message": "<可读信息>", "field_errors": {...}, ...}
"""

from rest_framework.views import exception_handler as drf_exception_handler


def error_payload(message, status, field_errors=None):
    """页面视图/中间件手工返回 JSON 错误时复用的统一结构（与 DRF 处理器一致）。"""
    return {
        "code": status,
        "message": message,
        "field_errors": field_errors or {},
        "detail": message,
        "error": message,
    }


def _flatten_message(data):
    if isinstance(data, str):
        return data
    if isinstance(data, list) and data and isinstance(data[0], str):
        return "; ".join(data)
    if isinstance(data, dict):
        for key in ("detail", "message", "error", "non_field_errors"):
            if key in data:
                value = data[key]
                if isinstance(value, list) and value:
                    return str(value[0])
                return str(value)
    return "请求处理失败"


def unified_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None:
        return None

    data = response.data
    message = _flatten_message(data)
    if isinstance(data, dict):
        field_errors = {
            k: v for k, v in data.items()
            if k not in ("detail", "message", "error")
        }
        payload = dict(data)
        payload.update({
            "code": response.status_code,
            "message": message,
            "field_errors": field_errors,
            "detail": message,
            "error": message,
        })
    else:
        payload = {
            "code": response.status_code,
            "message": message,
            "field_errors": {"non_field_errors": data}
            if isinstance(data, list) else {},
            "detail": message,
            "error": message,
        }
    response.data = payload
    return response
