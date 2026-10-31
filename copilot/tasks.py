"""异步执行：线程池 + 心跳 + 超时回收。

不引 Celery/Redis 的理由见开发方案 §3.2：单机 + SQLite 场景下，broker 的
运维成本高于收益。这里用进程内线程池（max_workers=2），并严格遵守
SQLite 的使用约束：线程进出各调一次 ``close_old_connections``，
执行器内部只做短事务。
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from .engine.executor import execute_run
from .models import CopilotRun

logger = logging.getLogger(__name__)

STALE_SECONDS = 120

_EXECUTOR = None


def _max_workers():
    return max(1, int(getattr(settings, 'COPILOT_MAX_WORKERS', 2)))


def get_executor():
    global _EXECUTOR
    if _EXECUTOR is None:
        _EXECUTOR = ThreadPoolExecutor(max_workers=_max_workers(),
                                       thread_name_prefix='copilot')
    return _EXECUTOR


def submit(run_id):
    get_executor().submit(_job, str(run_id))


def _job(run_id):
    close_old_connections()
    try:
        execute_run(run_id)
    except Exception:  # noqa: BLE001 - 线程内异常不能静默丢失
        logger.exception('Copilot 后台任务失败：%s', run_id)
    finally:
        close_old_connections()


def execute_sync(run_id):
    """同步执行（测试与本地调试用；线上默认走 submit）。"""
    return execute_run(run_id)


def reap_stale_runs(stale_seconds=STALE_SECONDS):
    """把心跳超时的 run 标为 timeout。

    pending 用 created_at 判断（还没开始跑，没有心跳），running 用 heartbeat_at。
    轮询接口会顺带调用一次，避免依赖外部定时任务。
    """
    now = timezone.now()
    threshold = now - timedelta(seconds=stale_seconds)
    running = CopilotRun.objects.filter(
        status=CopilotRun.Status.RUNNING, heartbeat_at__lt=threshold,
    ).update(status=CopilotRun.Status.TIMEOUT, finished_at=now,
             error_code='TIMEOUT', answer='执行超时，请重试')
    pending = CopilotRun.objects.filter(
        status=CopilotRun.Status.PENDING, created_at__lt=threshold,
    ).update(status=CopilotRun.Status.TIMEOUT, finished_at=now,
             error_code='TIMEOUT', answer='执行超时，请重试')
    return running + pending


def interrupt_unfinished():
    """进程启动时调用：上一轮进程退出遗留的 running 永远不会有人推进。"""
    return CopilotRun.objects.filter(
        status__in=[CopilotRun.Status.RUNNING, CopilotRun.Status.PENDING]
    ).update(status=CopilotRun.Status.INTERRUPTED,
             finished_at=timezone.now(), error_code='INTERRUPTED',
             answer='服务重启导致本次问答中断，请重新提问')
