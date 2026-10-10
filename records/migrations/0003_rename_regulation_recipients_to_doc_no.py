from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('records', '0002_alter_customercredit_customer_type'),
    ]

    operations = [
        migrations.RenameField(
            model_name='regulation',
            old_name='recipients',
            new_name='doc_no',
        ),
        migrations.AlterField(
            model_name='regulation',
            name='doc_no',
            field=models.CharField(blank=False, max_length=200, null=False, verbose_name='文件编号'),
        ),
    ]
