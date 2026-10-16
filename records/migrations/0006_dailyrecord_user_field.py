"""每日记录多用户数据隔离 · 迁移 A：新增归属用户字段（先允许 NULL，供历史数据回填）。

配套：0007 回填历史归属 → 0008 收紧为 NOT NULL。
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('records', '0005_operationlog'),
    ]

    operations = [
        migrations.AddField(
            model_name='dailyrecord',
            name='user',
            field=models.ForeignKey(
                blank=True,
                help_text='记录归属用户，查询/操作均按此字段隔离',
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='daily_records',
                to=settings.AUTH_USER_MODEL,
                verbose_name='归属用户',
            ),
        ),
        migrations.AddIndex(
            model_name='dailyrecord',
            index=models.Index(fields=['user', '-record_date'], name='idx_dailyrec_user_date'),
        ),
    ]
