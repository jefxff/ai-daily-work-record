"""AI 助手通知模板种子：每日简报与规则提醒。

走通知体系的模板而不是命令里拼字符串：这样标题/正文/邮件均可在
「通知中心 → 模板」里改文案，不必为改一句话发一次版本。

幂等：按 code get_or_create，已存在的模板不会被覆盖（管理员改过的文案保得住）。
"""
from django.db import migrations

TEMPLATES = [
    {
        "code": "copilot_daily_digest",
        "name": "AI 每日简报",
        "description": "每个工作日汇总到期/逾期、新增客诉、未完成日报与借阅超期",
        "title_template": "今日简报：{{ headline }}",
        "content_template": (
            "今天是 {{ date }}，业务简报已生成：{{ headline }}。"
            "点开即可查看明细并继续追问。"
        ),
        "email_subject_template": "【每日简报】{{ date }}：{{ headline }}",
        "email_body_template": (
            "{{ date }} 业务简报：{{ headline }}\n\n"
            "明细与追问：{{ link }}\n"
        ),
        "category": "business",
    },
    {
        "code": "copilot_alert",
        "name": "AI 业务异常提醒",
        "description": "确定性规则命中的异常（缺日报、客诉集中、逾期堆积）",
        "title_template": "{{ title }}",
        "content_template": "{{ title }}\n{{ body }}",
        "email_subject_template": "【业务提醒】{{ title }}",
        "email_body_template": "{{ title }}\n\n{{ body }}\n\n{% if link %}查看：{{ link }}\n{% endif %}",
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
        ("copilot", "0003_seed_copilot_tasks"),
        ("notifications", "0015_systemlog"),
    ]

    operations = [
        migrations.RunPython(seed_templates, remove_templates),
    ]
