"""OperationLog 增加操作人账号列，并回填历史行。

背景（一致性审计 P0-2）：`operator` 存的是展示名，各模块口径不同 —— 多数记用户名，
档案/证照/试样记 Profile 姓名。同一个人因此被记成「张三」和「zhangsan」两条，
`records/views_audit.py` 里按 operator 分组的「单小时高频」「导出爆发」会把一个
人拆成两个人，阈值永远打不到。

本迁移只加列 + 回填，**不修改** `operator` 本身（留痕的历史内容不重写）。
回填规则（尽力而为，不猜）：
1. operator 与某个 username 完全相同 → 直接取该 username；
2. operator 与某个 UserProfile.real_name 唯一匹配 → 取对应用户的 username；
3. 其余（IP、重名、已删用户）留空，聚合时自动回退到 operator。
"""

from django.db import migrations, models


def _unique_name_map(UserProfile):
    """real_name → username，只保留唯一映射（重名不猜）。"""
    seen = {}
    duplicated = set()
    for real_name, username in UserProfile.objects.values_list("real_name", "user__username"):
        real_name = (real_name or "").strip()
        if not real_name:
            continue
        if real_name in seen and seen[real_name] != username:
            duplicated.add(real_name)
        else:
            seen[real_name] = username
    for name in duplicated:
        seen.pop(name, None)
    return seen


def backfill(apps, schema_editor):
    OperationLog = apps.get_model("records", "OperationLog")
    User = apps.get_model("auth", "User")
    UserProfile = apps.get_model("accounts", "UserProfile")

    usernames = set(User.objects.values_list("username", flat=True))
    name_map = _unique_name_map(UserProfile)

    pending = []
    for log in OperationLog.objects.exclude(operator="").only("id", "operator", "operator_username").iterator():
        display = (log.operator or "").strip()
        if not display:
            continue
        if display in usernames:
            username = display
        else:
            username = name_map.get(display, "")
        if username and username != log.operator_username:
            log.operator_username = username
            pending.append(log)
        if len(pending) >= 1000:
            OperationLog.objects.bulk_update(pending, ["operator_username"])
            pending = []
    if pending:
        OperationLog.objects.bulk_update(pending, ["operator_username"])


def clear_backfill(apps, schema_editor):
    """反向：把派生列清空（不触碰 operator，历史可再推导）。"""
    OperationLog = apps.get_model("records", "OperationLog")
    OperationLog.objects.exclude(operator_username="").update(operator_username="")


class Migration(migrations.Migration):

    dependencies = [
        ("records", "0017_insurance_subject_one_off"),
        ("accounts", "0007_alter_apitoken_key_hash"),
    ]

    operations = [
        migrations.AddField(
            model_name="operationlog",
            name="operator_username",
            field=models.CharField(
                blank=True, db_index=True, default="", max_length=150,
                verbose_name="操作人账号",
            ),
        ),
        migrations.RunPython(backfill, clear_backfill),
    ]
