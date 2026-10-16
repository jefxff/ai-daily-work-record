"""内置通知模板种子数据（全量 6 类场景）。

- daily_record_missing     任务提醒：当日每日记录未填写
- daily_record_incomplete  任务提醒：每日记录过期未完成
- insurance_expiry         业务提醒：保险临近到期
- license_expiry           业务提醒：证照临近到期/年审
- archive_expiry           业务提醒：档案合同临近到期
- borrow_due               业务提醒：借阅归还到期/逾期
- system_announcement      系统公告：管理员公告推送
- sample_audit_result      业务提醒：试样审厂结果

幂等：按 code get_or_create，可重复执行；已存在的模板不会被覆盖。
"""
from django.db import migrations

TEMPLATES = [
    {
        "code": "daily_record_missing",
        "name": "当日每日记录未填写提醒",
        "description": "定时扫描：启用用户当日无任何每日记录时触发",
        "title_template": "今日每日记录待填写",
        "content_template": "{{ date }} 的每日记录尚未填写，请及时记录今日工作内容。",
        "email_subject_template": "【工作提醒】{{ date }} 每日记录待填写",
        "email_body_template": "{{ date }} 的每日记录尚未填写，请及时记录今日工作内容。\n\n点击处理：{{ link }}",
        "category": "task",
    },
    {
        "code": "daily_record_incomplete",
        "name": "每日记录过期未完成提醒",
        "description": "定时扫描 / 新建未完成记录时触发",
        "title_template": "每日记录仍未完成",
        "content_template": "记录于 {{ record_date }} 的每日记录（进度 {{ progress }}%）仍未完成：{{ work_content }}…",
        "email_subject_template": "【工作提醒】每日记录过期未完成",
        "email_body_template": "记录于 {{ record_date }} 的每日记录（进度 {{ progress }}%）仍未完成：{{ work_content }}…\n\n点击处理：{{ link }}",
        "category": "task",
    },
    {
        "code": "insurance_expiry",
        "name": "保险临近到期提醒",
        "description": "定时扫描：保单在提醒窗口内到期",
        "title_template": "保险临近到期",
        "content_template": "保险单「{{ title }}」（{{ company_name }}）将于 {{ end_date }} 到期，剩余 {{ days_left }} 天，请及时处理续保事宜。",
        "email_subject_template": "【到期提醒】保险单 {{ title }} 将于 {{ end_date }} 到期",
        "email_body_template": "保险单「{{ title }}」（{{ company_name }}）将于 {{ end_date }} 到期，剩余 {{ days_left }} 天，请及时处理续保事宜。\n\n点击处理：{{ link }}",
        "category": "business",
    },
    {
        "code": "license_expiry",
        "name": "证照到期/年审提醒",
        "description": "定时扫描：证照有效期或下次年审日在提醒窗口内",
        "title_template": "证照到期/年审提醒",
        "content_template": "证照「{{ license_name }}」（{{ code }}）将于 {{ expire_date }} {% if days_left == 0 %}今日{% else %}到期（剩余 {{ days_left }} 天）{% endif %}，请及时办理。",
        "email_subject_template": "【到期提醒】证照 {{ license_name }} 将于 {{ expire_date }} 到期",
        "email_body_template": "证照「{{ license_name }}」（{{ code }}）将于 {{ expire_date }} 到期（剩余 {{ days_left }} 天），请及时办理。\n\n点击处理：{{ link }}",
        "category": "business",
    },
    {
        "code": "archive_expiry",
        "name": "档案合同临近到期提醒",
        "description": "定时扫描：档案合同截止日期在提醒窗口内",
        "title_template": "档案合同临近到期",
        "content_template": "档案「{{ archive_name }}」的合同将于 {{ expire_date }} 到期，剩余 {{ days_left }} 天，请及时跟进。",
        "email_subject_template": "【到期提醒】档案合同 {{ archive_name }} 将于 {{ expire_date }} 到期",
        "email_body_template": "档案「{{ archive_name }}」的合同将于 {{ expire_date }} 到期，剩余 {{ days_left }} 天，请及时跟进。\n\n点击处理：{{ link }}",
        "category": "business",
    },
    {
        "code": "borrow_due",
        "name": "借阅归还提醒",
        "description": "定时扫描：借阅到期或已逾期未归还",
        "title_template": "借阅归还提醒",
        "content_template": "借阅人 {{ borrower }} 的借阅记录{% if overdue_days > 0 %}已逾期 {{ overdue_days }} 天{% else %}今日到期{% endif %}（应归还日期 {{ due_time }}），请及时归还。",
        "email_subject_template": "【归还提醒】借阅{% if overdue_days > 0 %}已逾期 {{ overdue_days }} 天{% else %}今日到期{% endif %}",
        "email_body_template": "借阅人 {{ borrower }} 的借阅记录{% if overdue_days > 0 %}已逾期 {{ overdue_days }} 天{% else %}今日到期{% endif %}（应归还日期 {{ due_time }}），请及时归还。\n\n点击处理：{{ link }}",
        "category": "business",
    },
    {
        "code": "system_announcement",
        "name": "系统公告",
        "description": "管理员手动推送的系统公告",
        "title_template": "{{ title }}",
        "content_template": "{{ content }}",
        "email_subject_template": "【系统公告】{{ title }}",
        "email_body_template": "{{ content }}\n\n{{ link }}",
        "category": "announcement",
    },
    {
        "code": "sample_audit_result",
        "name": "试样审厂结果通知",
        "description": "登记审厂记录后通知相关用户",
        "title_template": "审厂结果已登记",
        "content_template": "供应商「{{ partner_name }}」第 {{ round_no }} 轮审厂结果：{{ audit_result }}（{{ audit_no }}）。",
        "email_subject_template": "【业务提醒】{{ partner_name }} 审厂结果已登记",
        "email_body_template": "供应商「{{ partner_name }}」第 {{ round_no }} 轮审厂结果：{{ audit_result }}（{{ audit_no }}）。\n\n点击查看：{{ link }}",
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
        ("notifications", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_templates, remove_templates),
    ]
