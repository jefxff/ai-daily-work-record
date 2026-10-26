"""下载类流式响应跳过 gzip 的 GZipMiddleware。

备份包（`.zip.enc`，Fernet 密文为 base64 文本）本身已不可压缩，再套一层 gzip 只会：
1) 丢掉 Content-Length —— 浏览器下载面板拿不到文件大小，且响应靠「连接关闭」界定边界；
2) 对上百 MB 的包做一次无意义的压缩，白耗 CPU，还多一个可能出错的中间环节。

因此凡是带 `Content-Disposition: attachment` 的流式响应（备份包下载、归档导出等
已经是压缩格式的文件）一律原样返回，保留 Content-Length；其它页面照常压缩。
"""

from django.middleware.gzip import GZipMiddleware


class SkipGzipForDownloadMiddleware(GZipMiddleware):
    def process_response(self, request, response):
        if response.streaming and response.has_header("Content-Disposition"):
            return response
        return super().process_response(request, response)
