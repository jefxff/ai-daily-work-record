"""AI 助手治理类定时任务种子。

- ``copilot_prune``：每日 03:30 执行保留策略。策略表里写着"会话保留 90 天"，
  此前没有任何任务真的删过东西——这是把承诺落到执行的那一环。
- ``copilot_feedback_report``：每日 03:40 汇总点踩反馈（数据量小，按天跑
  便于在任务历史里看趋势；周报直接看最近 7 天即可）。

两点刻意为之：
1. 时间放在凌晨，避开 04:30 的审计日志清理与 05:00 的 VACUUM（SQLite 单写锁，
   撞在一起只会互相等锁）；
2. 幂等 get_or_create 按 code，不覆盖管理员改过的参数。
"""
from datetime import time

from django.db import migrations

SEED_TASKS = [
    {
        "code": "copilot_prune",
        "name": "AI 会话保留策略",
        "description": "清理超过保留期的 AI 会话与工具调用；外发审计日志按独立保留期清理",
        "enabled": True, "trigger_type": "daily", "run_time": time(3, 30),
        "interval_minutes": None, "params": {},
    },
    {
        "code": "copilot_feedback_report",
        "name": "AI 反馈汇总",
        "description": "统计点踩反馈与不满意率，输出 Top 问题（执行历史可在任务页查看）",
        "enabled": True, "trigger_type": "daily", "run_time": time(3, 40),
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
        ("copilot", "0002_next_stage_fields"),
        ("notifications", "0015_systemlog"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
