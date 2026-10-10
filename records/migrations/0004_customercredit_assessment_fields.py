from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('records', '0003_rename_regulation_recipients_to_doc_no'),
    ]

    operations = [
        migrations.AddField(
            model_name='customercredit',
            name='has_assessment_period',
            field=models.BooleanField(default=False, verbose_name='是否有业绩考核期'),
        ),
        migrations.AddField(
            model_name='customercredit',
            name='assessment_start_date',
            field=models.DateField(blank=True, null=True, verbose_name='考核期开始日期'),
        ),
        migrations.AddField(
            model_name='customercredit',
            name='assessment_end_date',
            field=models.DateField(blank=True, null=True, verbose_name='考核期结束日期'),
        ),
        migrations.AddField(
            model_name='customercredit',
            name='assessment_content',
            field=models.CharField(blank=True, default='', max_length=500, null=True, verbose_name='考核内容'),
        ),
    ]
