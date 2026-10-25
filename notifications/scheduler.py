"""通知模块定时任务调度器。

部署形态：web 进程（runserver）与独立进程（`manage.py run_scheduler`）分离。
- 每 30s 扫描 ScheduledTask 中已启用且到期的任务并串行执行；
- 手动「立即执行」（超管）与调度线程共用原子领取（exclude running），
  避免并发重复执行；running 超过 30 分钟视为僵死可重新领取；
- 执行结果（状态/耗时/输出摘要）持久化到 ScheduledTask，通知中心页面直接展示。

心跳：进程内 `HEARTBEATS` 只是本进程缓存；真正跨进程可见的是
`logs/.hb_worker` / `logs/.hb_scheduler` 两个心跳文件（`record_heartbeat` 落盘）。
web 进程与 run_scheduler 是两个进程，只读内存字典会永远看到「未运行」。

连续失败告警：同一任务连续失败达到 FAIL_STREAK_THRESHOLD 时通知 system.update 管理员。
计数采用**_模块级进程内计数** `_FAIL_STREAK`（键为 task.pk）——零 DB schema 约束下的
取舍：进程/服务重启后计数清零，语义为「**本次运行内**连续失败」，而非跨重启累计。
"""
import io
import logging
import os
import threading
import time
import traceback
from datetime import timedelta

from django.conf import settings
from django.core.management import call_command
from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone

from notifications.models import ScheduledTask, TaskRunLog

logger = logging.getLogger(__name__)

SCAN_INTERVAL_SECONDS = 30
RUNNING_STALE_MINUTES = 30      # running 超过该时长视为僵死，允许重新领取
ALIVE_WINDOW_SECONDS = 90       # 心跳超过该时长无更新视为线程不存活
MESSAGE_MAX_CHARS = 2000
RUN_LOG_KEEP_PER_TASK = 200     # 每任务保留的执行历史条数（超出自动裁剪最早的）

HEARTBEATS = {"worker": None, "scheduler": None}
_run_lock = threading.Lock()    # 串行执行任务，避免多任务并发压垮 SQLite

# 连续失败告警阈值：同一任务在本次运行内连续失败达到该次数即通知管理员。
FAIL_STREAK_THRESHOLD = 3
# 进程内连续失败计数（task.pk → 计数）。限制见模块 docstring：重启清零、仅本次运行语义。
_FAIL_STREAK = {}


def _heartbeat_path(key):
    """心跳文件路径（目录来自 settings.SCHEDULER_HEARTBEAT_DIR）。"""
    base = getattr(settings, "SCHEDULER_HEARTBEAT_DIR", None) \
        or os.path.join(str(settings.BASE_DIR), "logs")
    return os.path.join(str(base), f".hb_{key}")


def record_heartbeat(key):
    """更新本进程心跳并落盘；落盘失败只影响跨进程可见性，不抛异常。"""
    now = time.time()
    HEARTBEATS[key] = now
    try:
        path = _heartbeat_path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="ascii") as fh:
            fh.write(str(now))
    except OSError:
        logger.debug("心跳文件写入失败（key=%s）", key, exc_info=True)


def heartbeat_ts(key):
    """心跳时间戳：取本进程内存与心跳文件中的较大值；都没有返回 None。"""
    ts = HEARTBEATS.get(key)
    try:
        with open(_heartbeat_path(key), "r", encoding="ascii") as fh:
            value = float((fh.read() or "").strip() or 0)
        if ts is None or value > ts:
            ts = value
    except (OSError, ValueError):
        pass
    return ts


# 各命令允许的参数白名单（任务 code → 参数键）
COMMAND_PARAMS = {
    "check_expiry_notices": {"days"},
    "check_insurance_renewal": {"days"},
    "check_daily_incomplete": {"skip_history"},
    "notify_expire": {"purge_days"},
    "notify_stats": {"check"},
    "notify_knowledge_todo": {"days", "overdue_days"},
    "daily_backup": set(),
    # SQLite 维护（wal_checkpoint + VACUUM + ANALYZE），命令内部按 28 天节流
    "db_maintenance": set(),
    "cleanup_operation_log": {"days"},
}


def _alive(key):
    ts = heartbeat_ts(key)
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
        elif key in ("days", "purge_days", "interval_minutes", "overdue_days"):
            try:
                kwargs[key] = int(value)
            except (TypeError, ValueError):
                pass
    return kwargs


def _handle_task_result(task, ok, message):
    """维护进程内连续失败计数，达阈值时通知 system.update 管理员。

    - ok=True：清零该任务的连续失败计数并返回；
    - ok=False：计数 +1；达到 FAIL_STREAK_THRESHOLD 时向管理员逐个发通知。
      同一自然日同一任务只告警一次（靠 dedup_context 幂等）。

    通知发送整体容错：任何异常仅记日志，绝不影响任务主流程。
    """
    if ok:
        _FAIL_STREAK.pop(task.pk, None)
        return
    count = _FAIL_STREAK.get(task.pk, 0) + 1
    _FAIL_STREAK[task.pk] = count
    if count < FAIL_STREAK_THRESHOLD:
        return
    try:
        from . import services

        recipients = list(services.users_with_perm("system", "update"))
        context = {
            "task_name": task.name,
            "code": task.code,
            "fail_count": count,
            "last_message": (message or "")[:200],
        }
        dedup_context = f"task:{task.pk}:{timezone.localdate().isoformat()}"
        for user in recipients:
            services.emit(
                "scheduled_task_failure",
                recipient=user,
                context=context,
                dedup_context=dedup_context,
                link="/system/health/",
            )
    except Exception:
        logger.exception("定时任务连续失败告警发送失败（不影响任务主流程）")


def _prune_run_logs(task_pk):
    """按任务裁剪执行历史：仅保留最近 RUN_LOG_KEEP_PER_TASK 条。

    裁剪失败仅记日志：执行历史是审计辅助数据，不得影响任务主流程。
    """
    try:
        keep_ids = list(
            TaskRunLog.objects.filter(task_id=task_pk)
            .order_by("-finished_at", "-id")
            .values_list("id", flat=True)[:RUN_LOG_KEEP_PER_TASK]
        )
        TaskRunLog.objects.filter(task_id=task_pk).exclude(id__in=keep_ids).delete()
    except Exception:
        logger.exception("定时任务执行历史裁剪失败（task=%s）", task_pk)


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
    started_at = timezone.now()
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
        finished_at = timezone.now()
        ScheduledTask.objects.filter(pk=task.pk).update(
            last_status="success" if ok else "fail",
            last_message=message,
            last_duration_ms=duration_ms,
            last_run_at=finished_at,
            updated_at=finished_at,
        )
        # 执行历史留痕（每条一次），随后按任务裁剪至最近 N 条
        TaskRunLog.objects.create(
            task=task, code=task.code, source=source,
            status="success" if ok else "fail",
            duration_ms=duration_ms, message=message,
            started_at=started_at, finished_at=finished_at,
        )
        _prune_run_logs(task.pk)
        _handle_task_result(task, ok, message)
        close_old_connections()
    return True, ok, message


def run_due_tasks(should_stop=None) -> int:
    """执行一轮到期任务，返回本轮实际执行数量。

    web 进程内的调度线程与独立进程（manage.py run_scheduler）共用本函数，
    两条路径的执行语义完全一致；should_stop 为可选的中断判断（退出信号）。
    """
    executed = 0
    for task in ScheduledTask.objects.filter(enabled=True):
        if should_stop is not None and should_stop():
            break
        if not is_due(task):
            continue
        with _run_lock:
            task.refresh_from_db()
            if task.enabled and is_due(task):
                run_task(task)
                executed += 1
    return executed


def scheduler_loop():
    """调度主循环：串行执行到期任务（仅进程内模式使用）。"""
    while True:
        record_heartbeat("scheduler")
        try:
            close_old_connections()
            run_due_tasks()
        except Exception:
            logger.exception("通知定时任务调度轮询异常")
        time.sleep(SCAN_INTERVAL_SECONDS)

