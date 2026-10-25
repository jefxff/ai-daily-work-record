"""审计清理与数据库维护的定时任务种子（审查 H6 / W6）。

- cleanup_operation_log：每日 04:30 清理超期操作留痕与登录日志（保留 365 天）；
- db_maintenance：每日 05:00 触发，命令内部 28 天节流，实际执行
  wal_checkpoint(TRUNCATE) + VACUUM + ANALYZE。

为什么要有：留痕已补到全站，写入量必然上升；SQLite 删行不还盘，若不清理
并 VACUUM，库文件与每日备份包会同步无限膨胀（单盘写满会让业务写入失败）。
幂等：按 code get_or_create，不覆盖用户已改过的配置。
"""
from datetime import time

from django.db import migrations

SEED_TASKS = [
    {
        "code": "cleanup_operation_log",
        "name": "审计日志清理",
        # 不在种子参数里写死天数：留空即由命令读取 settings.OPERATION_LOG_RETENTION_DAYS，
        # 避免「settings 说 730、任务写 365」两套口径（审查发现的口径冲突）。
        "description": "每日清理超过保留期的操作留痕与登录日志（天数取 settings.OPERATION_LOG_RETENTION_DAYS）",
        "enabled": True, "trigger_type": "daily", "run_time": time(4, 30),
        "interval_minutes": None, "params": {},
    },
    {
        "code": "db_maintenance",
        "name": "数据库维护（VACUUM）",
        "description": "每日触发、命令内 28 天节流：WAL 检查点 + VACUUM + ANALYZE",
        "enabled": True, "trigger_type": "daily", "run_time": time(5, 0),
        "interval_minutes": None, "params": {},
    },
]


def seed(apps, schema_editor):
    ScheduledTask = apps.get_model("notifications", "ScheduledTask")
    for task in SEED_TASKS:
        ScheduledTask.objects.get_or_create(code=task["code"], defaults=task)


def unseed(apps, schema_editor):
    ScheduledTask = apps.get_model("notifications", "ScheduledTask")
    ScheduledTask.objects.filter(code__in=[t["code"] for t in SEED_TASKS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0013_seed_daily_backup"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
