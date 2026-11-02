"""HTTPS WSGI 服务器（单进程多线程 + 标准库 TLS，零新增运行时依赖）。

为什么不用现成方案（本项目约束：单机、单进程、Windows、不想引入 Web 服务器）：
- Django `runserver` 从 1.9 起不再支持 `--cert-file`；
- waitress 3.x **没有** TLS 支持（已核实其源码无 certfile/ssl_context 相关参数）；
- gunicorn/uwsgi 在本项目属禁用项（备份任务状态、互斥锁、调度心跳都是进程内实现，
  多 worker 会互不可见，见 settings.py「部署形态」注释）；
- stunnel / nginx 需要额外安装二进制，对「一个 bat 双击就跑」的现场太重。

因此这里用 **wsgiref + SSLContext**（都在标准库）搭一个线程化服务器：
- 单进程多线程：与 runserver 的并发模型一致，不破坏备份互斥与进程内状态；
- TLS 由 `ssl.SSLContext` 负责，证书由 `scripts/gen_dev_cert.py` 生成的
  本地 CA 签发（装一次根证书后浏览器不再告警）；
- 静态文件、媒体文件仍走 Django 自己的路由（/static/ 由 STATIC_ROOT 提供，
  /media/ 走受保护视图），此脚本不额外接管，保持行为一致。

用法：
    python scripts/serve_https.py                     # 0.0.0.0:8443
    python scripts/serve_https.py --port 9443
    python scripts/serve_https.py --host 127.0.0.1

性能说明：wsgiref 单请求处理能力弱于 waitress，但本项目是内网小团队（十几人）
的 CRUD 系统，瓶颈在 SQLite 单写者而不是 HTTP 解析。若将来并发显著上升，
可在前面加一层 stunnel/nginx 终止 TLS，本脚本即不再需要。
"""
import argparse
import os
import ssl
import sys
import threading
from socketserver import ThreadingMixIn
from wsgiref.simple_server import (
    ServerHandler as _ServerHandler,
    WSGIRequestHandler,
    WSGIServer,
    make_server,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

DEFAULT_CERT = os.path.join(BASE_DIR, "certs", "server.crt.pem")
DEFAULT_KEY = os.path.join(BASE_DIR, "certs", "server.key.pem")


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """线程化 WSGI 服务器：与 runserver 的并发模型保持一致（单进程、多线程）。"""

    daemon_threads = True
    allow_reuse_address = True
    # socketserver 默认 listen(5)。本服务器每条连接只服务一个请求（见
    # SingleRequestServerHandler），一页十几个静态资源就会同时涌进来；Windows
    # 在 accept 队列满时直接 RST，浏览器侧表现为随机的 ERR_CONNECTION_REFUSED。
    # 队列放大到 128 后可吃下并发突发。
    request_queue_size = 128
    # 与 HTTP 入口（work_record/server_patch.py）对齐：不暴露 wsgiref/Python 版本
    server_version = "WorkRecord"
    sys_version = ""


class SingleRequestServerHandler(_ServerHandler):
    """HTTP/1.1 响应，并**显式声明 Connection: close**。

    ⚠️ 不要移除下面 cleanup_headers 里的 Connection: close（2026-11-02 审查）：
    本服务器的连接语义曾经自相矛盾——

    - ``RequestHandler.handle()`` 覆盖了 ``BaseHTTPRequestHandler.handle()``，
      每条 TCP/TLS 连接**只处理一个请求**就 close；
    - 却又把 ``protocol_version`` 声明成 HTTP/1.1，且从不发 ``Connection`` 头。

    按 RFC 9112，HTTP/1.1 在没有 ``Connection: close`` 时默认长连接，浏览器会把
    这条已关闭的 socket 回收进连接池；下一次请求复用上去，在收到任何响应字节之前
    就撞上 RST → ``ERR_CONNECTION_CLOSED``（Chrome/Edge 显示“网络问题”）。
    curl 每次新建连接所以从未暴露；socket 层实测：同一连接发第二个请求必失败。

    这里如实告知客户端“本条连接到此为止”。wsgiref 自己做不到长连接：它没有实现
    chunked 编码（``wsgiref/handlers.py`` 里只有一句 XXX 注释），而 Django 的
    非 gzip 响应又不带 Content-Length，响应体只能靠“关闭连接”来界定边界——强行
    长连接会让浏览器把响应体读成下一个响应的状态行。所以关闭是唯一正确选择。

    ``http_version = "1.1"`` 保留（响应行用 1.1），只是不再假装支持复用。

    另注：备份中心「下载」报“无法下载 - 网络问题”的主因是**证书未受信任 +
    <a download> 属性**（Chromium 会掐死证书错误站点上的下载管理器请求，请求
    根本不出网），与本文件无关；见 backup_center.html 同日注释。
    """

    http_version = "1.1"
    # Server 响应头脱敏（审计 BE-03）：wsgiref 用 server_software 拼这个头
    # （simple_server.py: ServerHandler.server_software = "WSGIServer/0.2 CPython/x"），
    # 改 server_version/sys_version 不生效，必须覆盖 server_software 本身
    server_software = "WorkRecord"

    def cleanup_headers(self):
        super().cleanup_headers()
        # 必须在 cleanup_headers 之后写：父类此时可能刚补上 Content-Length
        self.headers["Connection"] = "close"


class RequestHandler(WSGIRequestHandler):
    """请求处理：每条连接只服务一个请求（响应侧显式 Connection: close）。"""

    protocol_version = "HTTP/1.1"

    def handle(self):
        # 与响应头的 Connection: close 保持一致：这里本来就没有
        # BaseHTTPRequestHandler.handle() 的请求循环，显式写出来避免读者误会。
        self.close_connection = True
        self.raw_requestline = self.rfile.readline(65537)
        if len(self.raw_requestline) > 65536:
            self.requestline = ""
            self.request_version = ""
            self.command = ""
            self.send_error(414)
            return
        if not self.parse_request():
            return
        handler = SingleRequestServerHandler(
            self.rfile, self.wfile, self.get_stderr(), self.get_environ(),
            multithread=True,
        )
        handler.request_handler = self
        handler.run(self.server.get_app())

    def log_message(self, fmt, *args):  # noqa: A003 - 与基类签名一致
        # 交给 Django 的日志体系（settings.LOGGING 已配置请求日志），这里降噪
        if os.environ.get("HTTPS_SERVER_ACCESS_LOG") == "1":
            super().log_message(fmt, *args)


def build_ssl_context(certfile: str, keyfile: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    # 会话复用，减少内网反复握手的开销
    ctx.options |= ssl.OP_NO_COMPRESSION
    return ctx


def main() -> int:
    ap = argparse.ArgumentParser(description="用 HTTPS 启动 Django（单进程多线程）")
    ap.add_argument(
        "--host", default="0.0.0.0",  # nosec B104 — 内网多机访问需要监听全网卡，与 runserver 0.0.0.0 一致
        help="监听地址（默认 0.0.0.0，供内网其它机器访问；仅本机用 127.0.0.1）",
    )
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--cert", default=os.environ.get("HTTPS_CERT", DEFAULT_CERT))
    ap.add_argument("--key", default=os.environ.get("HTTPS_KEY", DEFAULT_KEY))
    args = ap.parse_args()

    if not (os.path.isfile(args.cert) and os.path.isfile(args.key)):
        print("缺少证书文件，请先运行： python scripts/gen_dev_cert.py", file=sys.stderr)
        return 2

    # HTTPS 模式下必须让 Django 打开 Secure/__Host- cookie 与 SSL 重定向开关，
    # 否则浏览器收不到会话 cookie，表现为「登录后仍是未登录」。
    os.environ.setdefault("DJANGO_HTTPS_ONLY", "1")
    os.environ.setdefault("DJANGO_DEBUG", "0")

    import django
    from django.core.wsgi import get_wsgi_application

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "work_record.settings")
    django.setup()
    application = get_wsgi_application()

    def force_https(environ, start_response):
        """强制标记请求为 HTTPS。

        实测（Python 3.13）：仅在 ``WSGIRequestHandler.get_environ()`` 里改
        ``wsgi.url_scheme`` 不生效 —— wsgiref 在后续阶段会按 socket 重建该键，
        于是 Django 仍认为请求是 http，配合 ``SECURE_SSL_REDIRECT`` 会出现
        无限 301。放在应用入口改是唯一可靠的位置。
        """
        environ["wsgi.url_scheme"] = "https"
        return application(environ, start_response)

    ctx = build_ssl_context(args.cert, args.key)
    httpd = make_server(
        args.host, args.port, force_https,
        server_class=ThreadingWSGIServer, handler_class=RequestHandler,
    )
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    print(f"HTTPS 服务已启动：https://{args.host}:{args.port}/")
    print(f"证书：{args.cert}")
    print("按 Ctrl+C 停止")
    try:
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        while t.is_alive():
            t.join(timeout=1)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
