"""请求性能采集中间件（批次 C）。

置于 MIDDLEWARE 链最外层：包裹整条请求（会话/认证/视图/模板全部计入），
用 ``connection.execute_wrapper`` 逐条统计 SQL 耗时，请求结束后落一行请求级指标。

异常安全：任何采集/落库错误都只记日志，绝不影响请求本身；
对静态资源、媒体文件、favicon、探针等噪声路径直接跳过（零开销）。
"""
import logging
import time

from django.db import connection

from . import metrics

logger = logging.getLogger(__name__)


class RequestMetricsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not metrics.is_enabled() or metrics.should_skip(request.path):
            return self.get_response(request)

        started = time.perf_counter()
        state = {"db_count": 0, "db_time_ms": 0.0, "slow_query": ""}
        try:
            with connection.execute_wrapper(metrics.make_query_collector(state)):
                response = self.get_response(request)
        except Exception:
            duration_ms = (time.perf_counter() - started) * 1000
            self._record(request, 500, duration_ms, state)
            raise
        duration_ms = (time.perf_counter() - started) * 1000
        self._record(request, getattr(response, "status_code", 0), duration_ms, state)
        return response

    def _record(self, request, status_code, duration_ms, state):
        try:
            user = ""
            account = getattr(request, "user", None)
            if getattr(account, "is_authenticated", False):
                user = account.get_username()
            metrics.record(
                method=request.method,
                path=request.path,
                status_code=status_code,
                duration_ms=duration_ms,
                db_count=state["db_count"],
                db_time_ms=state["db_time_ms"],
                slow_query=state.get("slow_query", ""),
                user=user,
            )
        except Exception:
            logger.exception("请求性能指标采集失败（不影响请求）")
