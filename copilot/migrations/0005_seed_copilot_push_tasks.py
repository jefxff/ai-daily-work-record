"""AI 助手主动推送类定时任务种子（B3-1 / B3-2）。

- ``copilot_daily_digest``：工作日 09:00（命令内部会跳过周末）。
  挑在早上而不是凌晨：简报的价值在于"今天要处理的"，凌晨生成到被看到时
  已经隔了一夜，数字可能已经变了。
- ``copilot_alerts``：每天 09:10，晚于简报 10 分钟，错开 SQLite 写峰值。

两个任务默认**关闭**：先让管理员在通知中心看一眼 dry-run 输出、确认口径，
再点启用。自动启用的推送如果没有被校准过，第一次误报就会把信任用光。
"""
from datetime import time

from django.db import migrations

SEED_TASKS = [
    {
        "code": "copilot_daily_digest",
        "name": "AI 每日简报",
        "description": "每个工作日汇总到期/逾期、新增客诉、未完成日报与借阅超期，写入 AI 会话并推送",
        "enabled": False, "trigger_type": "daily", "run_time": time(9, 0),
        "interval_minutes": None,
        "params": {"confirm": True},
    },
    {
        "code": "copilot_alerts",
        "name": "AI 业务异常提醒",
        "description": "缺日报 / 客诉集中 / 逾期堆积三条规则命中时提醒（默认干跑，需在参数里确认）",
        "enabled": False, "trigger_type": "daily", "run_time": time(9, 10),
        "interval_minutes": None,
        "params": {"confirm": True},
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
        ("copilot", "0004_seed_copilot_templates"),
        ("notifications", "0015_systemlog"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
