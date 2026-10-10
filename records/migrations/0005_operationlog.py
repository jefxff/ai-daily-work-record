from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('records', '0004_customercredit_assessment_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='OperationLog',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('operation_time', models.DateTimeField(auto_now_add=True, verbose_name='操作时间')),
                ('operator', models.CharField(max_length=100, verbose_name='操作人')),
                ('module_type', models.CharField(max_length=50, verbose_name='模块类型')),
                ('operation_type', models.CharField(max_length=20, verbose_name='操作类型')),
                ('object_identifier', models.CharField(max_length=200, verbose_name='对象标识')),
                ('change_summary', models.TextField(blank=True, null=True, verbose_name='变更摘要')),
            ],
            options={
                'verbose_name': '操作留痕',
                'verbose_name_plural': '操作留痕',
                'ordering': ['-operation_time'],
            },
        ),
    ]
