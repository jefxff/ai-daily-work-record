from .models import OperationLog


def log_operation(request, module_type, operation_type, object_identifier, change_summary=''):
    """记录操作留痕：优先记录登录用户名，未登录时 fallback 到 IP 地址"""
    operator = '未知'
    if request:
        if hasattr(request, 'user') and request.user.is_authenticated:
            operator = request.user.get_username()
        else:
            # 未登录请求 fallback 到 IP（RBAC 全站登录后理论上不会走到这里）
            xff = request.META.get('HTTP_X_FORWARDED_FOR')
            if xff:
                operator = xff.split(',')[0].strip()
            else:
                operator = request.META.get('REMOTE_ADDR', '未知')
    OperationLog.objects.create(
        operator=operator,
        module_type=module_type,
        operation_type=operation_type,
        object_identifier=object_identifier,
        change_summary=change_summary or '',
    )
