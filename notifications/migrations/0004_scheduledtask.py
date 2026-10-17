"""新增通知定时任务配置表，并种子化 4 个内置任务。

此前 4 个扫描类命令依赖外部 Windows 计划任务/cron，项目内无任何调度配置，
导致到期提醒等从未执行；改为进程内调度器后按本表配置执行。
"""
from datetime import time

from django.db import migrations, models


SEED_TASKS = [
    {
        "code": "check_expiry_notices",
        "name": "到期提醒扫描",
        "description": "保险 / 证照 / 档案合同 / 借阅归还到期与逾期提醒",
        "enabled": True, "trigger_type": "daily", "run_time": time(8, 0),
        "interval_minutes": None, "params": {"days": 7},
    },
    {
        "code": "check_daily_incomplete",
        "name": "每日记录提醒",
        "description": "当日未填写提醒 + 过期未完成记录提醒",
        "enabled": True, "trigger_type": "daily", "run_time": time(9, 0),
        "interval_minutes": None, "params": {"skip_history": False},
    },
    {
        "code": "notify_expire",
        "name": "通知过期兜底",
        "description": "pending 超时置为 expired；可选清理历史已读/作废通知",
        "enabled": True, "trigger_type": "daily", "run_time": time(3, 30),
        "interval_minutes": None, "params": {"purge_days": 0},
    },
    {
        "code": "notify_stats",
        "name": "队列健康巡检",
        "description": "发送成功率/队列堆积/死信阈值检查，越限告警超管",
        "enabled": False, "trigger_type": "interval", "run_time": None,
        "interval_minutes": 720, "params": {"check": True},
    },
]


def seed_tasks(apps, schema_editor):
    ScheduledTask = apps.get_model("notifications", "ScheduledTask")
    for item in SEED_TASKS:
        ScheduledTask.objects.get_or_create(code=item["code"], defaults=item)


def remove_tasks(apps, schema_editor):
    ScheduledTask = apps.get_model("notifications", "ScheduledTask")
    ScheduledTask.objects.filter(
        code__in=[item["code"] for item in SEED_TASKS],
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0003_notification_batch_id"),
    ]

    operations = [
        migrations.CreateModel(
            name="ScheduledTask",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(max_length=50, unique=True, verbose_name="命令编码")),
                ("name", models.CharField(max_length=100, verbose_name="任务名称")),
                ("description", models.CharField(blank=True, default="", max_length=200, verbose_name="用途说明")),
                ("enabled", models.BooleanField(default=False, verbose_name="启用")),
                ("trigger_type", models.CharField(
                    choices=[("daily", "每天定时"), ("interval", "固定间隔")],
                    default="daily", max_length=10, verbose_name="触发方式")),
                ("run_time", models.TimeField(blank=True, null=True, verbose_name="每日执行时间")),
                ("interval_minutes", models.IntegerField(blank=True, null=True, verbose_name="间隔分钟数")),
                ("params", models.JSONField(blank=True, default=dict, verbose_name="任务参数")),
                ("last_run_at", models.DateTimeField(blank=True, null=True, verbose_name="上次执行时间")),
                ("last_status", models.CharField(default="idle", max_length=10, verbose_name="上次执行状态")),
                ("last_message", models.TextField(blank=True, default="", verbose_name="上次执行结果")),
                ("last_duration_ms", models.IntegerField(blank=True, null=True, verbose_name="上次耗时(ms)")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="更新时间")),
            ],
            options={
                "verbose_name": "通知定时任务",
                "verbose_name_plural": "通知定时任务",
                "ordering": ["code"],
            },
        ),
        migrations.RunPython(seed_tasks, remove_tasks),
    ]
