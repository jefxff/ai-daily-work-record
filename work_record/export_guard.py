"""导出类重操作的并发闸门与行数上限（审查 H5 / B3）。

背景（单机 runserver 场景下的真实约束）：
- runserver 是**单进程多线程**，没有请求超时、没有并发上限；
- 导出会把整本工作簿构建在内存里（openpyxl），一行几十个单元格对象；
- 单写者 SQLite 下，导出期间的长查询还会拖慢别人的写操作。

因此这里给「导出」加两道硬约束：
1. 同时最多 ``EXPORT_MAX_CONCURRENCY``（默认 2）个导出在跑，超出立刻 429，
   提示稍后重试 —— 而不是把线程池占满导致整站鼠标转圈；
2. 单次导出不超过 ``EXPORT_MAX_ROWS``（默认 30000）行，超出返回 413 并提示
   缩小筛选范围 —— 避免一次导出把进程内存打爆（OOM 会让 web 与调度进程一起死）。

注意：这不是「限制功能」，而是把不可控的失败（OOM / 整站卡死）换成
可理解、可重试的明确答复。需要更大批量时改 settings 里的两个常量即可。
"""
import threading

from django.conf import settings
from django.http import HttpResponse

DEFAULT_MAX_CONCURRENCY = 2
DEFAULT_MAX_ROWS = 30000

_semaphore = None
_sem_lock = threading.Lock()


def _get_semaphore():
    """惰性创建信号量：确保只在首次导出时才按 settings 取值。"""
    global _semaphore
    if _semaphore is None:
        with _sem_lock:
            if _semaphore is None:
                limit = max(1, int(getattr(
                    settings, "EXPORT_MAX_CONCURRENCY", DEFAULT_MAX_CONCURRENCY)))
                _semaphore = threading.BoundedSemaphore(limit)
    return _semaphore


class ExportSlot:
    """非阻塞占用一个导出槽位。

    用法（手动进出，便于在视图里「拿不到就立刻返回」）：
        slot = ExportSlot()
        if not slot.__enter__():        # 返回是否抢到槽位
            return export_busy_response()
        try:
            ...
        finally:
            slot.__exit__(None, None, None)
    """

    def __init__(self):
        self.acquired = False

    def __enter__(self) -> bool:
        self.acquired = _get_semaphore().acquire(blocking=False)
        return self.acquired

    def __exit__(self, exc_type, exc, tb):
        if self.acquired:
            _get_semaphore().release()
            self.acquired = False
        return False


def export_busy_response():
    return HttpResponse(
        "当前正在进行的导出任务较多，请稍后再试。",
        status=429,
        content_type="text/plain; charset=utf-8",
    )


def export_too_large_response(total: int, limit: int):
    return HttpResponse(
        f"本次导出行数（{total}）超过单次上限（{limit}）。请缩小筛选范围后重试。",
        status=413,
        content_type="text/plain; charset=utf-8",
    )


def max_export_rows() -> int:
    return max(1, int(getattr(settings, "EXPORT_MAX_ROWS", DEFAULT_MAX_ROWS)))
