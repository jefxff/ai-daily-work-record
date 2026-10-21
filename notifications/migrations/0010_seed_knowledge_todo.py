"""知识待办到期提醒：种子通知模板 + 定时任务（幕布化批次 5）。

- 通知模板 knowledge_todo_due：提醒文档 owner 待办临期/逾期；
- 定时任务 notify_knowledge_todo：每日 08:30，扫描窗口
  [今天-7, 今天+3]，经进程内调度器执行（不建外部计划任务）。

幂等：均按 code get_or_create，可重复执行；已存在不会被覆盖。
"""
from datetime import time

from django.db import migrations


TEMPLATES = [
    {
        "code": "knowledge_todo_due",
        "name": "知识待办到期提醒",
        "description": "知识库大纲待办节点临期/逾期时提醒文档 owner",
        "title_template": "知识待办{{ days_left_word }}：{{ todo_text }}",
        "content_template": "文档《{{ doc_title }}》中的待办「{{ todo_text }}」"
                            "{{ days_left_word }}（{{ due_date }}），请及时处理。",
        "email_subject_template": "【知识待办】{{ todo_text }} {{ days_left_word }}",
        "email_body_template": "文档《{{ doc_title }}》中的待办「{{ todo_text }}」"
                               "{{ days_left_word }}（{{ due_date }}）。\n\n"
                               "点击处理：{{ link }}",
        "category": "task",
    },
]

SEED_TASK = {
    "code": "notify_knowledge_todo",
    "name": "知识待办提醒",
    "description": "扫描知识库大纲待办节点，临期/逾期提醒文档 owner",
    "enabled": True, "trigger_type": "daily", "run_time": time(8, 30),
    "interval_minutes": None,
    "params": {"days": 3, "overdue_days": 7},
}


def seed(apps, schema_editor):
    NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")
    for spec in TEMPLATES:
        NotificationTemplate.objects.get_or_create(
            code=spec["code"],
            defaults={
                "name": spec["name"],
                "description": spec["description"],
                "title_template": spec["title_template"],
                "content_template": spec["content_template"],
                "email_subject_template": spec["email_subject_template"],
                "email_body_template": spec["email_body_template"],
                "category": spec["category"],
                "is_active": True,
            },
        )
    ScheduledTask = apps.get_model("notifications", "ScheduledTask")
    ScheduledTask.objects.get_or_create(code=SEED_TASK["code"], defaults=SEED_TASK)


def unseed(apps, schema_editor):
    NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")
    NotificationTemplate.objects.filter(
        code__in=[t["code"] for t in TEMPLATES]).delete()
    ScheduledTask = apps.get_model("notifications", "ScheduledTask")
    ScheduledTask.objects.filter(code=SEED_TASK["code"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0009_seed_complaint_closed_template"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
