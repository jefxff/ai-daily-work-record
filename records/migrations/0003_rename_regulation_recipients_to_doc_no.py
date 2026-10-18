# 破坏性操作说明（补记，2026-10 审计 #44）
# RenameField: Regulation.recipients → doc_no（收件人 → 文号）
# 原因：该列在台账里始终存放制度文号，原字段名与实际语义不符。
# 数据去向：原地改名，数据不丢失。
# 回滚代价：可逆（反向 RenameField）。

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
