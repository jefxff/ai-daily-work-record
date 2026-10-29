"""附件上传 / 删除 / 缩略图视图（全模块通用，按 RBAC 模块 key 鉴权）。

URL 形如 ``/attachments/upload/<app_label>/<model>/<pk>/``：先按
``MODEL_MODULE_MAP`` 解析业务对象的归属模块，再做 ``update`` 动作权限判定，
避免「拿 A 模块的权限给 B 模块的对象传文件」。
"""

import os

from django.contrib.contenttypes.models import ContentType
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from accounts.registry import MODULE_MAP
from accounts.services import has_perm
from work_record.error_handler import error_payload

from .models import ATTACHMENT_CATEGORIES, MODEL_MODULE_MAP, Attachment
from .validators import validate_attachment


def _resolve(app_label, model, pk):
    """URL → (业务对象, 模块key)。未登记模型/不存在记录一律 404。"""
    ct = get_object_or_404(ContentType, app_label=app_label, model=model.lower())
    module_key = MODEL_MODULE_MAP.get((ct.app_label, ct.model))
    if not module_key:
        raise Http404("该业务对象不支持附件")
    obj = get_object_or_404(ct.model_class(), pk=pk)
    return obj, module_key


def _log_attachment(request, module_key, action, obj_id, summary):
    """附件操作留痕：模块名取 RBAC 注册表中文名，动作走 audit_vocab 已登记词。"""
    from records.audit import log_operation  # 延迟导入：records 反向依赖本 app

    name = MODULE_MAP[module_key]["name"]
    log_operation(
        request, name, action, f"[{name}]下的记录#{obj_id}", summary
    )


@require_POST
def attachment_upload(request, app_label, model, pk):
    """为业务记录上传附件（支持一次多选，逐个校验，返回成功/失败清单）。"""
    if not getattr(request.user, "is_authenticated", False):
        return JsonResponse(
            {"ok": False, **error_payload("身份验证未通过，请先登录。", 401)}, status=401
        )
    obj, module_key = _resolve(app_label, model, pk)
    if not has_perm(request.user, module_key, "update"):
        return JsonResponse(
            {"ok": False, **error_payload("权限不足，无法上传附件。", 403)}, status=403
        )

    files = request.FILES.getlist("files")
    if not files:
        return JsonResponse(
            {"ok": False, **error_payload("未选择文件", 422)}, status=422
        )

    # 业务分类：只接受该模块已登记的值，非法值按「未分类」处理
    allowed = {v for v, _ in ATTACHMENT_CATEGORIES.get(module_key, [])}
    category = request.POST.get("category", "").strip()
    if category not in allowed:
        category = ""

    created, errors = [], []
    for f in files:
        err = validate_attachment(f)
        if err:
            errors.append(f"{f.name}：{err}")
            continue
        Attachment.objects.create(
            content_type=ContentType.objects.get_for_model(obj),
            object_id=obj.pk,
            file=f,
            original_name=(f.name or "")[:255],
            size=getattr(f, "size", 0) or 0,
            category=category,
            uploaded_by=request.user
            if getattr(request.user, "is_authenticated", False)
            else None,
        )
        created.append(f.name)

    if created:
        names = "、".join(n[:30] for n in created[:10])
        _log_attachment(
            request, module_key, "上传附件", obj.pk,
            f"上传附件 {len(created)} 个：{names}",
        )

    if errors and not created:
        return JsonResponse(
            {"ok": False, **error_payload("；".join(errors), 422)}, status=422
        )
    msg = f"成功上传 {len(created)} 个附件"
    if errors:
        msg += f"；{len(errors)} 个失败：{'；'.join(errors)}"
    return JsonResponse({"ok": True, "message": msg, "created": len(created), "errors": errors})


@require_POST
def attachment_delete(request, pk):
    """删除附件（物理文件 + 数据行），按附件归属模块判定 update 权限。"""
    if not getattr(request.user, "is_authenticated", False):
        return JsonResponse(
            {"ok": False, **error_payload("身份验证未通过，请先登录。", 401)}, status=401
        )
    att = get_object_or_404(Attachment.objects.select_related("content_type"), pk=pk)
    module_key = MODEL_MODULE_MAP.get((att.content_type.app_label, att.content_type.model))
    if not module_key or not has_perm(request.user, module_key, "update"):
        return JsonResponse(
            {"ok": False, **error_payload("权限不足，无法删除附件。", 403)}, status=403
        )
    obj_id, name = att.object_id, att.original_name or (att.file.name if att.file else "")
    if att.file:
        att.file.delete(save=False)
    att.delete()
    _log_attachment(request, module_key, "删除附件", obj_id, f"删除附件: {name}")
    return JsonResponse({"ok": True, "message": "附件已删除"})


def attachment_thumb(request, pk):
    """图片附件缩略图：首次访问用 Pillow 生成并缓存到 media/thumbs/，之后直接回图。

    与 archive 的附件缩略图同一策略（透明图转白底，避免黑底），缓存文件名加
    ``att`` 前缀避免与档案缩略图混淆。read 权限即可访问。
    """
    from django.conf import settings

    att = get_object_or_404(Attachment.objects.select_related("content_type"), pk=pk)
    module_key = MODEL_MODULE_MAP.get((att.content_type.app_label, att.content_type.model))
    if not module_key or not has_perm(request.user, module_key, "read"):
        from django.core.exceptions import PermissionDenied

        raise PermissionDenied("无权访问该文件")
    if not att.is_image or not att.file:
        raise Http404

    thumb_dir = os.path.join(settings.MEDIA_ROOT, "thumbs")
    os.makedirs(thumb_dir, exist_ok=True)
    thumb_path = os.path.join(thumb_dir, f"att{att.pk}.jpg")
    if not os.path.exists(thumb_path):
        from PIL import Image

        with Image.open(att.file.path) as img:
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                bg = Image.new("RGB", img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1])
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((480, 480))
            img.save(thumb_path, "JPEG", quality=85)
    return FileResponse(open(thumb_path, "rb"), content_type="image/jpeg")
