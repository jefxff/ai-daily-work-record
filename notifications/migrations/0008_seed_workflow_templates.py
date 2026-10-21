"""业务闭环通知模板种子（批次 3：待办工作台通知接入）。

- sample_terminated       业务提醒：样品终止（系统判定不通过 / 手动终止）
- license_review_failed   业务提醒：证照年审未通过（年审 failed 此前无任何出口）

幂等：按 code get_or_create，可重复执行；已存在的模板不会被覆盖。
"""
from django.db import migrations

TEMPLATES = [
    {
        "code": "sample_terminated",
        "name": "样品终止通知",
        "description": "样品被系统判定不通过或手动终止时，通知试样管理可读用户",
        "title_template": "样品 {{ sample_no }} 已终止",
        "content_template": "样品「{{ material_name }}/{{ model }}」（{{ sample_no }}）已终止。终止原因：{{ reason }}；操作人：{{ operator }}。",
        "email_subject_template": "【业务提醒】样品 {{ sample_no }} 已终止",
        "email_body_template": "样品「{{ material_name }}/{{ model }}」（{{ sample_no }}）已终止。\n终止原因：{{ reason }}\n操作人：{{ operator }}\n\n点击查看：{{ link }}",
        "category": "business",
    },
    {
        "code": "license_review_failed",
        "name": "证照年审未通过通知",
        "description": "年审登记结果为「未通过」时，通知档案管理可读用户安排整改与重新送审",
        "title_template": "证照年审未通过",
        "content_template": "证照「{{ license_name }}」（{{ code }}）{{ review_date }} 年审未通过（经办人：{{ reviewer }}），请尽快安排整改与重新送审。{% if remark %}备注：{{ remark }}{% endif %}",
        "email_subject_template": "【业务提醒】证照 {{ license_name }} 年审未通过",
        "email_body_template": "证照「{{ license_name }}」（{{ code }}）{{ review_date }} 年审未通过（经办人：{{ reviewer }}），请尽快安排整改与重新送审。\n{% if remark %}备注：{{ remark }}\n{% endif %}\n点击查看：{{ link }}",
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
        ("notifications", "0007_requestmetric"),
    ]

    operations = [
        migrations.RunPython(seed_templates, remove_templates),
    ]
