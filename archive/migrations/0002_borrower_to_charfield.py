# 破坏性操作说明（补记，2026-10 审计 #44）
# RenameField: borrower(外键→用户) → borrower(CharField)
# 原因：借阅人可以是系统外人员（供应商、外部律师），无法强制关联 User。
# 数据去向：原外键值以「用户名字符串」写入同名列，用户删除后仍可读。
# 回滚代价：不可逆（字符串无法可靠还原为用户 id），如需回滚须从备份恢复。

"""借阅人由系统用户外键改为自由输入文本（保留既有数据：外键转为用户名文本）。

迁移步骤：
1. 新增 borrower_name 文本字段（临时）
2. 数据迁移：把旧外键指向用户的 username 写入 borrower_name
3. 删除旧外键字段
4. 将 borrower_name 重命名为 borrower
"""

from django.db import migrations, models


def copy_borrower_names(apps, schema_editor):
    BorrowRecord = apps.get_model("archive", "BorrowRecord")
    for rec in BorrowRecord.objects.all():
        user = rec.borrower  # 迁移时仍为外键，返回 User 或 None
        rec.borrower_name = getattr(user, "username", "") or "" if user else ""
        rec.save(update_fields=["borrower_name"])


class Migration(migrations.Migration):

    dependencies = [
        ("archive", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="borrowrecord",
            name="borrower_name",
            field=models.CharField(default="", help_text="借阅人姓名（可为系统外人员）", max_length=50, verbose_name="借阅人"),
            preserve_default=False,
        ),
        migrations.RunPython(copy_borrower_names, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="borrowrecord",
            name="borrower",
        ),
        migrations.RenameField(
            model_name="borrowrecord",
            old_name="borrower_name",
            new_name="borrower",
        ),
    ]
