"""每日自动备份：种子进程内调度任务（不建外部计划任务 / cron）。

备份本身沿用备份中心的全量链路（SQLite 在线快照 + media + 加密 + 操作留痕），
由 notifications.scheduler 每日 02:30 触发，失败时按既有「连续失败告警」规则
通知 system.update 管理员。

幂等：按 code get_or_create，重复执行不会覆盖用户已改过的配置。
"""
from datetime import time

from django.db import migrations

SEED_TASK = {
    "code": "daily_backup",
    "name": "每日自动备份",
    "description": "每日凌晨生成一份全量加密备份，保留周期由运维在备份中心清理",
    "enabled": True, "trigger_type": "daily", "run_time": time(2, 30),
    "interval_minutes": None, "params": {},
}


def seed(apps, schema_editor):
    ScheduledTask = apps.get_model("notifications", "ScheduledTask")
    ScheduledTask.objects.get_or_create(code=SEED_TASK["code"], defaults=SEED_TASK)


def unseed(apps, schema_editor):
    ScheduledTask = apps.get_model("notifications", "ScheduledTask")
    ScheduledTask.objects.filter(code=SEED_TASK["code"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0012_refine_insurance_lapsed_template"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
