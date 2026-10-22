"""保险应续未续提醒文案优化：非车险标的名称与险种相同时不再重复展示险种。"""

from django.db import migrations

CONTENT = (
    "保险标的「{{ subject }}」"
    "{% if insurance_type and insurance_type != subject %}（{{ insurance_type }}，"
    "{{ company_name }}）{% else %}（{{ company_name }}）{% endif %}"
    "的保障已于 {{ end_date }} 到期，已断保 {{ overdue_days }} 天"
    "仍未录入续保，请及时处理。"
)
EMAIL_BODY = (
    "保险标的「{{ subject }}」"
    "{% if insurance_type and insurance_type != subject %}（{{ insurance_type }}，"
    "{{ company_name }}）{% else %}（{{ company_name }}）{% endif %}"
    "的保障已于 {{ end_date }} 到期，已断保 {{ overdue_days }} 天"
    "仍未录入续保，请及时处理。\n\n点击处理：{{ link }}"
)


def forward(apps, schema_editor):
    NotificationTemplate = apps.get_model("notifications", "NotificationTemplate")
    NotificationTemplate.objects.filter(code="insurance_lapsed").update(
        content_template=CONTENT, email_body_template=EMAIL_BODY,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0011_seed_insurance_renewal"),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
