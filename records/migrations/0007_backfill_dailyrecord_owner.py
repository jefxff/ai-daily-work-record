"""每日记录多用户数据隔离 · 迁移 B：历史数据归属回填。

系统上线以来仅 jefxff（超级管理员）实际使用，所有无归属的历史记录回填给它；
jefxff 不存在时回退任一启用中的超管。仅 UPDATE、无 DELETE，幂等可重跑。
"""

from django.db import migrations


def backfill_owner(apps, schema_editor):
    DailyRecord = apps.get_model('records', 'DailyRecord')
    User = apps.get_model('auth', 'User')

    owner = (
        User.objects.filter(username='jefxff', is_active=True).first()
        or User.objects.filter(is_superuser=True, is_active=True).order_by('id').first()
    )
    if owner is None:
        # 极端情况（无任何用户）：保持 NULL，由 0008 收紧迁移显式报错兜底
        return

    updated = DailyRecord.objects.filter(user__isnull=True).update(user=owner)
    print(f'[0007] 每日记录历史归属回填 -> {owner.username}: {updated} 条')


def un_backfill(apps, schema_editor):
    DailyRecord = apps.get_model('records', 'DailyRecord')
    DailyRecord.objects.update(user=None)


class Migration(migrations.Migration):

    dependencies = [
        ('records', '0006_dailyrecord_user_field'),
    ]

    operations = [
        migrations.RunPython(backfill_owner, un_backfill),
    ]
