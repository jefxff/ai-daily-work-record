"""通知模块进程内定时任务调度器。

单进程部署（runserver）下随 web 进程启动后台线程：
- 每 30s 扫描 ScheduledTask 中已启用且到期的任务并串行执行；
- 手动「立即执行」（超管）与调度线程共用原子领取（exclude running），
  避免并发重复执行；running 超过 30 分钟视为僵死可重新领取；
- 执行结果（状态/耗时/输出摘要）持久化到 ScheduledTask，通知中心页面直接展示。

心跳：HEARTBEATS 供视图判断 worker / scheduler 线程是否存活（90s 内有活动视为存活）。
"""
import io
import logging
import threading
import time
import traceback
from datetime import timedelta

from django.core.management import call_command
from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone

from notifications.models import ScheduledTask

logger = logging.getLogger(__name__)

SCAN_INTERVAL_SECONDS = 30
RUNNING_STALE_MINUTES = 30      # running 超过该时长视为僵死，允许重新领取
ALIVE_WINDOW_SECONDS = 90       # 心跳超过该时长无更新视为线程不存活
MESSAGE_MAX_CHARS = 2000

HEARTBEATS = {"worker": None, "scheduler": None}
_run_lock = threading.Lock()    # 串行执行任务，避免多任务并发压垮 SQLite

# 各命令允许的参数白名单（任务 code → 参数键）
COMMAND_PARAMS = {
    "check_expiry_notices": {"days"},
    "check_daily_incomplete": {"skip_history"},
    "notify_expire": {"purge_days"},
    "notify_stats": {"check"},
}


def _alive(key):
    ts = HEARTBEATS.get(key)
    return ts is not None and (time.time() - ts) < ALIVE_WINDOW_SECONDS


def worker_alive():
    """后台发送线程是否存活。"""
    return _alive("worker")


def scheduler_alive():
    """定时任务调度线程是否存活。"""
    return _alive("scheduler")


def is_due(task, now=None):
    """任务是否到期（仅判断已启用任务）。"""
    if not task.enabled:
        return False
    now = now or timezone.now()
    if task.trigger_type == task.TriggerType.DAILY:
        if not task.run_time:
            return False
        local_now = timezone.localtime(now)
        if local_now.time() < task.run_time:
            return False
        if task.last_run_at is None:
            return True
        return timezone.localtime(task.last_run_at).date() < local_now.date()
    # 固定间隔
    if not task.interval_minutes or task.interval_minutes <= 0:
        return False
    if task.last_run_at is None:
        return True
    return (now - task.last_run_at).total_seconds() >= task.interval_minutes * 60


def next_run_at(task, now=None):
    """下次预计执行时间；未启用或触发配置不完整返回 None。"""
    if not task.enabled:
        return None
    now = now or timezone.now()
    local_now = timezone.localtime(now)
    if task.trigger_type == task.TriggerType.DAILY:
        if not task.run_time:
            return None
        candidate = local_now.replace(
            hour=task.run_time.hour, minute=task.run_time.minute,
            second=0, microsecond=0,
        )
        if task.last_run_at and timezone.localtime(task.last_run_at).date() >= local_now.date():
            candidate += timedelta(days=1)
        elif candidate <= local_now:
            return now  # 今天时间点已过且当日未跑：即将执行
        return candidate
    if not task.interval_minutes or task.interval_minutes <= 0:
        return None
    if task.last_run_at is None:
        return now
    return task.last_run_at + timedelta(minutes=task.interval_minutes)


def _command_kwargs(task):
    """按白名单过滤并校正参数类型，映射为 call_command 关键字参数。"""
    allowed = COMMAND_PARAMS.get(task.code, set())
    params = task.params or {}
    kwargs = {}
    for key in allowed:
        if key not in params:
            continue
        value = params[key]
        if key in ("skip_history", "check"):
            kwargs[key] = bool(value)
        elif key in ("days", "purge_days", "interval_minutes"):
            try:
                kwargs[key] = int(value)
            except (TypeError, ValueError):
                pass
    return kwargs


def run_task(task, source="schedule"):
    """原子领取并同步执行一次任务；返回 (是否执行, 是否成功, 结果摘要)。

    领取条件：当前非 running，或 running 已僵死（超过 30 分钟未更新）。
    """
    stale_cutoff = timezone.now() - timedelta(minutes=RUNNING_STALE_MINUTES)
    claimed = ScheduledTask.objects.filter(pk=task.pk).filter(
        ~Q(last_status="running") | Q(updated_at__lt=stale_cutoff),
    ).update(last_status="running", updated_at=timezone.now())
    if not claimed:
        return False, False, "任务正在执行中，请稍后再试"
    task.refresh_from_db()

    started = time.monotonic()
    buf = io.StringIO()
    ok = True
    try:
        close_old_connections()
        call_command(task.code, stdout=buf, stderr=io.StringIO(),
                     **_command_kwargs(task))
    except SystemExit as exc:  # notify_stats --check 越限时 SystemExit(1)
        ok = not exc.code
        buf.write(f"\n（退出码 {exc.code}）")
    except Exception:
        ok = False
        buf.write("\n" + "\n".join(traceback.format_exc().splitlines()[-5:]))
        logger.exception("定时任务 %s 执行失败", task.code)
    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
        output = buf.getvalue().strip()
        message = output[-MESSAGE_MAX_CHARS:] or ("执行成功" if ok else "执行失败（无输出）")
        ScheduledTask.objects.filter(pk=task.pk).update(
            last_status="success" if ok else "fail",
            last_message=message,
            last_duration_ms=duration_ms,
            last_run_at=timezone.now(),
            updated_at=timezone.now(),
        )
        close_old_connections()
    return True, ok, message


def scheduler_loop():
    """调度主循环：串行执行到期任务。"""
    while True:
        HEARTBEATS["scheduler"] = time.time()
        try:
            close_old_connections()
            for task in ScheduledTask.objects.filter(enabled=True):
                if not is_due(task):
                    continue
                with _run_lock:
                    task.refresh_from_db()
                    if task.enabled and is_due(task):
                        run_task(task)
        except Exception:
            logger.exception("通知定时任务调度轮询异常")
        time.sleep(SCAN_INTERVAL_SECONDS)
