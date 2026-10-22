"""隐藏开发服务器版本横幅（审计 P3-6）。

Django runserver 基于 wsgiref，`Server:` 响应头由 BaseHTTPRequestHandler 写入，
应用层中间件无法删除。这里在启动前替换 WSGIServer 的版本字符串，使响应变为
`Server: WorkRecord`，不再暴露 Python/Django 精确版本。
"""

import logging

logger = logging.getLogger(__name__)


def harden_server_banner() -> None:
    try:
        import wsgiref.simple_server as simple_server

        # `Server:` 头由 ServerHandler.server_software 直接写出（wsgiref 模块常量），
        # 改服务器/处理器类的版本属性都不会影响它，必须改这里。
        simple_server.ServerHandler.server_software = "WorkRecord"

        simple_server.WSGIServer.server_version = "WorkRecord"
        simple_server.WSGIServer.sys_version = ""
        simple_server.WSGIRequestHandler.server_version = "WorkRecord"
        simple_server.WSGIRequestHandler.sys_version = ""

        # Django 的处理器子类在导入时就继承了取值，若已被导入则就地覆盖
        try:
            from django.core.servers import basehttp

            basehttp.WSGIRequestHandler.server_version = "WorkRecord"
            basehttp.WSGIRequestHandler.sys_version = ""
        except Exception:
            pass
    except Exception:
        logger.debug("无法替换 WSGI 服务器版本横幅", exc_info=True)
