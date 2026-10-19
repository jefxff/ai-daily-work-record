"""内置通知模板种子：定时任务连续失败告警（scheduled_task_failure）。

批次 A / A4：定时任务（ScheduledTask）在本次运行内连续失败达到阈值时，
由 notifications/scheduler.py 的 _handle_task_result 触发本模板通知管理员。

幂等：按 code get_or_create，可重复执行；已存在的模板不会被覆盖。
"""
from django.db import migrations

TEMPLATES = [
    {
        "code": "scheduled_task_failure",
        "name": "定时任务连续失败告警",
        "description": "同一任务连续失败达到阈值时通知管理员",
        "title_template": "定时任务连续失败",
        "content_template": "定时任务「{{ task_name }}」（{{ code }}）已连续失败 {{ fail_count }} 次，最近一次输出：{{ last_message }}…",
        "email_subject_template": "【系统告警】定时任务 {{ task_name }} 连续失败 {{ fail_count }} 次",
        "email_body_template": "定时任务「{{ task_name }}」（{{ code }}）已连续失败 {{ fail_count }} 次，最近一次输出：{{ last_message }}…\n\n点击处理：{{ link }}",
        "category": "task",
    },
]


def seed_templates(apps, schema_editor):
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


def remove_templates(apps, schema_editor):
    NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")
    NotificationTemplate.objects.filter(
        code__in=[t["code"] for t in TEMPLATES]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0004_scheduledtask"),
    ]

    operations = [
        migrations.RunPython(seed_templates, remove_templates),
    ]
