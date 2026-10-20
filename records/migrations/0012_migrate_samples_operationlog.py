"""存量 samples 操作留痕统一并入 records.OperationLog（模块=试样管理）。

操作留痕全站归一后，试样模块的留痕统一写 records.OperationLog；
本迁移把原 samples_operationlog 表中的存量记录按以下映射搬运：

- created_at              -> operation_time（bulk_create 会因 auto_now_add 覆盖为
                             当前时间，随后用 bulk_update 恢复原始操作时间）
- operator                -> operator
- action                  -> operation_type（create/update/delete/status
                             → 新增/编辑/删除/状态变更）
- target_type#target_repr -> object_identifier（历史英文对象类型归一为中文）
- summary                 -> change_summary
- ip                      -> 丢弃（records.OperationLog 无对应字段）

正向搬运、反向 no-op（历史留痕不回删，避免误伤统一后新写入的记录）。
"""
from django.db import migrations, transaction

ACTION_MAP = {
    "create": "新增",
    "update": "编辑",
    "delete": "删除",
    "status": "状态变更",
}

# 历史直写代码中使用的英文对象类型 → 页面端中文对象类型
TARGET_TYPE_MAP = {
    "Sample": "样品主档",
    "AuditRecord": "审厂记录",
}


def forwards(apps, schema_editor):
    Source = apps.get_model("samples", "OperationLog")
    Target = apps.get_model("records", "OperationLog")

    objs = []
    for row in Source.objects.all().order_by("id").iterator():
        target_type = TARGET_TYPE_MAP.get(row.target_type, row.target_type)
        objs.append(Target(
            operator=(row.operator or "").strip()[:100],
            module_type="试样管理",
            operation_type=ACTION_MAP.get(row.action, row.action),
            object_identifier=f"{target_type}#{row.target_repr}"[:200],
            change_summary=row.summary or "",
            operation_time=row.created_at,
        ))
    if not objs:
        return
    for obj in objs:
        obj.pk = None
    # bulk_create 的 pre_save(add=True) 会把 auto_now_add 字段的值就地覆盖为
    # 当前时间，因此先暂存原始时间，create 之后再回填，最后 bulk_update 落库
    # （bulk_update 走 pre_save(add=False)，不会再次触发 auto_now_add）
    original_times = [obj.operation_time for obj in objs]
    with transaction.atomic():
        Target.objects.bulk_create(objs, batch_size=500)
        for obj, t in zip(objs, original_times):
            obj.operation_time = t
        Target.objects.bulk_update(objs, ["operation_time"], batch_size=500)


def backwards(apps, schema_editor):
    pass  # 历史留痕不回删


class Migration(migrations.Migration):

    dependencies = [
        ("records", "0011_operationlog_after_json_operationlog_before_json"),
        ("samples", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
