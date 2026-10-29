"""附件服务的公共查询与上下文helper（视图 / 模板 / 造数共用）。"""

from django.contrib.contenttypes.models import ContentType
from django.db import models

from accounts.services import has_perm

from .models import ATTACHMENT_CATEGORIES, ATTACHMENT_MAX_SIZE, MODEL_MODULE_MAP, Attachment


def module_key_for(obj):
    """业务对象 → RBAC 模块 key；未登记返回空串（调用方应拒绝）。"""
    if obj is None:
        return ""
    ct = ContentType.objects.get_for_model(obj)
    return MODEL_MODULE_MAP.get((ct.app_label, ct.model), "")


def attachments_for(obj):
    """业务对象的全部附件（新→旧），带出上传人避免 N+1。"""
    ct = ContentType.objects.get_for_model(obj)
    return Attachment.objects.filter(content_type=ct, object_id=obj.pk).select_related(
        "uploaded_by"
    )


def panel_context(request, obj):
    """附件面板模板所需的 context（详情页视图直接 update 进 render context）。

    额外提供 att_app_label / att_model_name：Django 的 {% url %} 不接受
    ``obj._meta.app_label`` 这类下划线开头的变量，须由视图侧解析好传入。
    """
    module_key = module_key_for(obj)
    user = getattr(request, "user", None)
    return {
        "attachments": attachments_for(obj),
        "attachment_categories": ATTACHMENT_CATEGORIES.get(module_key, []),
        "can_manage_attachments": bool(module_key)
        and has_perm(user, module_key, "update"),
        "attachment_max_mb": ATTACHMENT_MAX_SIZE // 1024 // 1024,
        "att_app_label": obj._meta.app_label,
        "att_model_name": obj._meta.model_name,
    }


def inject_counts(objects):
    """列表页行钩子：为对象列表批量注入 ``attachment_count`` 属性。

    一次 GROUP BY 取完本页所有对象的附件数，避免每行一次 COUNT（N+1）。
    调用方（records.crud.make_list_view 的 row_hook）在分页后调用。
    """
    objects = list(objects)
    if not objects:
        return objects
    ct = ContentType.objects.get_for_model(objects[0])
    counts = dict(
        Attachment.objects.filter(
            content_type=ct, object_id__in=[o.pk for o in objects]
        )
        .values_list("object_id")
        .annotate(total=models.Count("id"))
    )
    for o in objects:
        o.attachment_count = counts.get(o.pk, 0)
    return objects


def delete_for_object(obj, obj_id=None):
    """删除业务对象的全部附件（物理文件 + 数据行）。

    供删除视图的 after_delete 钩子调用：``obj.delete()`` 之后实例 pk 已被置
    None，故允许调用方显式传入删除前的 obj_id（records.crud 已取出并透传）。
    """
    pk = obj_id if obj_id is not None else obj.pk
    if pk is None:
        return 0
    ct = ContentType.objects.get_for_model(obj)
    removed = 0
    for att in Attachment.objects.filter(content_type=ct, object_id=pk):
        if att.file:
            att.file.delete(save=False)  # 先删磁盘文件，再删数据行
        att.delete()
        removed += 1
    return removed
