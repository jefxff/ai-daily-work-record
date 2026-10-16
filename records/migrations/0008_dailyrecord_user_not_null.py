"""每日记录多用户数据隔离 · 迁移 C：收紧归属字段为 NOT NULL。

回填完成后在数据库层面杜绝"无归属记录"，创建路径漏绑定时会立即报错而非静默裸奔。
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('records', '0007_backfill_dailyrecord_owner'),
    ]

    operations = [
        migrations.AlterField(
            model_name='dailyrecord',
            name='user',
            field=models.ForeignKey(
                blank=False,
                help_text='记录归属用户，查询/操作均按此字段隔离',
                null=False,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='daily_records',
                to=settings.AUTH_USER_MODEL,
                verbose_name='归属用户',
            ),
        ),
    ]
