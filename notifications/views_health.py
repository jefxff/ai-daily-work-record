"""系统健康面板（只读）。

面向具备 system.read 权限的管理员，聚合展示：
- 健康探测（work_record.health.probe_health：数据库 / 迁移 / 日志目录）；
- 调度线程心跳（scheduler.HEARTBEATS 原始时间戳 + 存活判定）；
- 定时任务状态（ScheduledTask：上次执行结果 / 下次预计）；
- 备份操作历史（sysbackup.models.BackupOperationLog 最近 20 条）；
- 磁盘占用（总/已用/可用/百分比 + db.sqlite3 + backups/media 目录大小）。

全部为只读查询，不写入任何数据。
"""
import os
import shutil
import time
from datetime import datetime

from django.conf import settings
from django.shortcuts import render
from django.utils import timezone

from accounts.decorators import perm_required
from sysbackup.models import BackupOperationLog
from work_record.health import probe_health

from . import scheduler
from .models import ScheduledTask
from .views_metrics import _PROCESS_START

_CHECK_LABELS = {
    "database": "数据库连接",
    "migrations": "数据库迁移",
    "log_dir": "日志目录可写",
}


def _human_size(num):
    """字节数格式化为人类可读（如 1.2 GB）；未知返回「未知」。"""
    if num is None:
        return "未知"
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _fmt_uptime(seconds):
    """秒数格式化为「X天X小时X分」；用于进程运行时长展示。"""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} 天 {hours} 小时 {minutes} 分"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分"


def _dir_size(path):
    """目录总字节数；路径不存在或读取异常返回 None（容错，不抛异常）。"""
    try:
        if not path or not os.path.isdir(path):
            return None
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
        return total
    except Exception:
        return None


def _fmt_heartbeat(ts):
    """HEARTBEATS 原始时间戳（time.time()）→ 本地时间字符串；None 显示「无」。"""
    if ts is None:
        return "无"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "无"


@perm_required("system", "read")
def system_health(request):
    checks, healthy = probe_health()

    worker_alive = scheduler.worker_alive()
    scheduler_alive = scheduler.scheduler_alive()
    heartbeats = [
        {
            "label": "后台发送线程",
            "alive": worker_alive,
            "time": _fmt_heartbeat(scheduler.HEARTBEATS.get("worker")),
        },
        {
            "label": "定时调度线程",
            "alive": scheduler_alive,
            "time": _fmt_heartbeat(scheduler.HEARTBEATS.get("scheduler")),
        },
    ]

    check_items = [
        {"key": key, "label": _CHECK_LABELS.get(key, key), "value": checks.get(key)}
        for key in ("database", "migrations", "log_dir")
    ]

    tasks = [
        {"task": task, "next_run_at": scheduler.next_run_at(task)}
        for task in ScheduledTask.objects.all()
    ]

    backups = list(BackupOperationLog.objects.all()[:20])

    usage = shutil.disk_usage(str(settings.BASE_DIR))
    percent = round(usage.used / usage.total * 100, 1) if usage.total else 0
    disk = {
        "total": _human_size(usage.total),
        "used": _human_size(usage.used),
        "free": _human_size(usage.free),
        "percent": percent,
    }

    db_path = os.path.join(str(settings.BASE_DIR), "db.sqlite3")
    try:
        db_size = _human_size(os.path.getsize(db_path)) if os.path.isfile(db_path) else "未知"
    except OSError:
        db_size = "未知"

    backups_dir = os.path.join(str(settings.BASE_DIR), "backups")
    media_root = str(settings.MEDIA_ROOT)

    return render(request, "notifications/system_health.html", {
        "checks": checks,
        "check_items": check_items,
        "healthy": healthy,
        "heartbeats": heartbeats,
        "worker_alive": worker_alive,
        "scheduler_alive": scheduler_alive,
        "tasks": tasks,
        "backups": backups,
        "disk": disk,
        "db_size": db_size,
        "backups_dir": backups_dir,
        "backups_dir_size": _human_size(_dir_size(backups_dir)),
        "media_root": media_root,
        "media_root_size": _human_size(_dir_size(media_root)),
        # L9：进程运行时长从匿名探针迁到本页（仅 system.read 管理员可见）
        "uptime_seconds": int(time.monotonic() - _PROCESS_START),
        "uptime_text": _fmt_uptime(int(time.monotonic() - _PROCESS_START)),
        "now": timezone.now(),
    })
