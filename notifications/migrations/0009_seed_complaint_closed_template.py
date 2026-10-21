"""客诉闭环通知模板种子（批次 4：P2 客诉闭环）。

- complaint_closed  业务提醒：客诉已闭环

幂等：按 code get_or_create，可重复执行；已存在的模板不会被覆盖。
"""
from django.db import migrations

TEMPLATES = [
    {
        "code": "complaint_closed",
        "name": "客诉闭环通知",
        "description": "客户客诉确认闭环时，通知登记人与责任业务员",
        "title_template": "客诉已闭环：{{ customer_name }}",
        "content_template": "客户「{{ customer_name }}」的客诉已于 {{ closed_at }} 闭环（结论：{{ closure_result }}）。闭环人：{{ closed_by }}。{% if closure_note %}说明：{{ closure_note }}{% endif %}",
        "email_subject_template": "【业务提醒】客诉已闭环：{{ customer_name }}",
        "email_body_template": "客户「{{ customer_name }}」的客诉已于 {{ closed_at }} 闭环。\n闭环结论：{{ closure_result }}\n闭环人：{{ closed_by }}\n{% if closure_note %}闭环说明：{{ closure_note }}\n{% endif %}\n点击查看：{{ link }}",
        "category": "business",
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
        ("notifications", "0008_seed_workflow_templates"),
    ]

    operations = [
        migrations.RunPython(seed_templates, remove_templates),
    ]
