"""保险续保预警（阶段 3）：种子应续未续模板 + 续保预警定时任务。

- 通知模板 insurance_lapsed：标的断保后提醒（临期复用既有 insurance_expiry）；
- 定时任务 check_insurance_renewal：每日 08:10，提前 30 天提醒临期待续，
  并按周/月提醒应续未续，经进程内调度器执行（不建外部计划任务）。

幂等：均按 code get_or_create，可重复执行；已存在不会被覆盖。
"""
from datetime import time

from django.db import migrations


TEMPLATES = [
    {
        "code": "insurance_lapsed",
        "name": "保险应续未续提醒",
        "description": "保险标的已过保障截止日且未录入续保时提醒",
        "title_template": "保险应续未续：{{ subject }}",
        "content_template": "保险标的「{{ subject }}」（{{ insurance_type }}，{{ company_name }}）"
                            "的保障已于 {{ end_date }} 到期，已断保 {{ overdue_days }} 天"
                            "仍未录入续保，请及时处理。",
        "email_subject_template": "【续保预警】{{ subject }} 已断保 {{ overdue_days }} 天",
        "email_body_template": "保险标的「{{ subject }}」（{{ insurance_type }}，{{ company_name }}）"
                               "的保障已于 {{ end_date }} 到期，已断保 {{ overdue_days }} 天"
                               "仍未录入续保，请及时处理。\n\n点击处理：{{ link }}",
        "category": "business",
    },
]

SEED_TASK = {
    "code": "check_insurance_renewal",
    "name": "保险续保预警",
    "description": "按标的与保单年度扫描临期待续 / 应续未续并提醒",
    "enabled": True, "trigger_type": "daily", "run_time": time(8, 10),
    "interval_minutes": None, "params": {"days": 30},
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
        ("notifications", "0010_seed_knowledge_todo"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
