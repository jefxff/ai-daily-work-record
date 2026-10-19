"""档案域视图（CRUD/移动/附件/缩略图/删除/二维码/操作日志）。

从 views.py 拆出的独立业务域。URL 配置仍通过 views.py 门面 re-export 引用，
URL 名称与行为完全不变。
"""

import io
import os

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from accounts.decorators import perm_required
from records.crud import _per_page
from records.models import OperationLog

from .forms import ArchiveForm, validate_archive_file
from .models import Archive, ArchiveFile
from .services.common import _is_ajax, _log


@login_required
@perm_required("archive", "create")
def archive_create(request):
    form = ArchiveForm(request.POST or None, request.FILES or None)
    if form.is_valid():
        try:
            # 柜/层/格唯一由 DB 兜底（评审 R1）：表单校验是"先查后写"，并发/双击时
            # 两条请求都会通过 .exists() 检查，必须由事务 + 唯一约束最终把关。
            with transaction.atomic():
                arc = form.save(commit=False)
                arc.creator = request.user
                arc.save()
        except IntegrityError:
            form.add_error(None, "该存放位置（柜/层/格）已被占用，请重新选择")
        else:
            _log(request, "新增", f"档案#{arc.id}", f"新增档案: {arc.code} - {arc.name}（{arc.location_path}）")
            if _is_ajax(request):
                return JsonResponse({"ok": True, "message": f"档案 {arc.code} 已录入"})
            return redirect("archive:archive_detail", pk=arc.pk)
    if _is_ajax(request) and request.method == "POST":
        html = render(request, "archive/_archive_form_modal.html", {"form": form, "mode": "create"}).content.decode()
        return JsonResponse({"ok": False, "html": html}, status=422)
    # GET 预填柜/层/格（从空位入口进入）
    cab_id = request.GET.get("cabinet")
    layer = request.GET.get("layer")
    cell = request.GET.get("cell")
    if cab_id and layer and cell:
        try:
            form.initial["cabinet"] = int(cab_id)
            form.initial["layer"] = int(layer)
            form.initial["cell"] = int(cell)
        except ValueError:
            pass
    if _is_ajax(request):
        html = render(request, "archive/_archive_form_modal.html", {"form": form, "mode": "create"}).content.decode()
        return JsonResponse({"ok": True, "html": html})
    return render(request, "archive/archive_form.html", {"form": form, "mode": "create"})


@login_required
@perm_required("archive", "read")
def archive_detail(request, pk):
    arc = get_object_or_404(Archive, pk=pk)
    borrows = arc.borrow_records.all()[:10]
    # 仅显示该档案自身的操作历史（精确匹配对象标识，避免误匹配其他模块/其他ID）
    logs = (
        OperationLog.objects.filter(module_type="档案管理", object_identifier=f"档案#{arc.id}")
        .order_by("-operation_time")[:20]
    )
    context = {
        "archive": arc,
        "files": arc.files.all(),
        "borrows": borrows,
        "logs": logs,
        # 批量导入的档案可能未分配档案柜，cabinet 为 None 时不可取 free_cells
        "free_cells": arc.cabinet.free_cells if arc.cabinet_id else None,
    }
    return render(request, "archive/archive_detail.html", context)


@login_required
@perm_required("archive", "update")
def archive_edit(request, pk):
    arc = get_object_or_404(Archive, pk=pk)
    form = ArchiveForm(request.POST or None, request.FILES or None, instance=arc)
    if form.is_valid():
        try:
            # 同上：编辑也改柜/层/格，DB 唯一约束兜底并发占用
            with transaction.atomic():
                arc = form.save()
        except IntegrityError:
            form.add_error(None, "该存放位置（柜/层/格）已被占用，请重新选择")
        else:
            _log(request, "编辑", f"档案#{arc.id}", f"编辑档案: {arc.code} - {arc.name}")
            if _is_ajax(request):
                return JsonResponse({"ok": True, "message": "档案已更新"})
            return redirect("archive:archive_detail", pk=arc.pk)
    if _is_ajax(request) and request.method == "POST":
        html = render(request, "archive/_archive_form_modal.html", {"form": form, "mode": "edit", "archive": arc}).content.decode()
        return JsonResponse({"ok": False, "html": html}, status=422)
    if _is_ajax(request):
        html = render(request, "archive/_archive_form_modal.html", {"form": form, "mode": "edit", "archive": arc}).content.decode()
        return JsonResponse({"ok": True, "html": html})
    return render(request, "archive/archive_form.html", {"form": form, "mode": "edit", "archive": arc})


@login_required
@perm_required("archive", "update")
def archive_move(request, pk):
    """跨空位移动档案位置。"""
    arc = get_object_or_404(Archive, pk=pk)
    form = ArchiveForm(request.POST or None, instance=arc, exclude_cell=False)
    if form.is_valid():
        try:
            # 跨空位移动同属"先查后写"：事务 + DB 唯一约束兜底两个请求抢同一空位
            with transaction.atomic():
                arc = form.save()
        except IntegrityError:
            form.add_error(None, "该存放位置（柜/层/格）已被占用，请重新选择")
        else:
            _log(request, "移动", f"档案#{arc.id}", f"移动档案: {arc.code} -> {arc.location_path}")
            if _is_ajax(request):
                return JsonResponse({"ok": True, "message": "档案已移动"})
            return redirect("archive:archive_detail", pk=arc.pk)
    return render(request, "archive/archive_move.html", {"form": form, "archive": arc})


@login_required
@perm_required("archive", "update")
def archive_files_upload(request, pk):
    """为档案上传电子附件（支持一次多选，逐个校验类型/大小，成功的保存并返回错误清单）。"""
    arc = get_object_or_404(Archive, pk=pk)
    if request.method != "POST":
        return JsonResponse({"ok": False, "message": "仅支持 POST"}, status=405)
    files = request.FILES.getlist("files")
    if not files:
        return JsonResponse({"ok": False, "message": "未选择文件"}, status=422)
    created, errors = [], []
    for f in files:
        err = validate_archive_file(f)
        if err:
            errors.append(f"{f.name}：{err}")
            continue
        ArchiveFile.objects.create(archive=arc, file=f)
        created.append(f.name)
    if created:
        _log(request, "上传附件", f"档案#{arc.id}",
             f"上传附件: {arc.code} - {arc.name}（{len(created)} 个：{'、'.join(created)}）")
    if errors and not created:
        return JsonResponse({"ok": False, "message": "；".join(errors)}, status=422)
    msg = f"成功上传 {len(created)} 个附件"
    if errors:
        msg += f"；{len(errors)} 个失败：{'；'.join(errors)}"
    return JsonResponse({"ok": True, "message": msg, "created": len(created), "errors": errors})


@login_required
@perm_required("archive", "update")
def archive_file_delete(request, pk):
    """删除档案附件（同时删除物理文件）。"""
    af = get_object_or_404(ArchiveFile.objects.select_related("archive"), pk=pk)
    arc = af.archive
    name = af.file_basename
    if af.file:
        af.file.delete(save=False)  # 删除磁盘文件
    af.delete()
    _log(request, "删除附件", f"档案#{arc.id}", f"删除附件: {arc.code} - {name}")
    if _is_ajax(request):
        return JsonResponse({"ok": True, "message": "附件已删除"})
    return redirect("archive:archive_detail", pk=arc.pk)


@login_required
@perm_required("archive", "read")
def archive_file_thumb(request, pk):
    """图片附件缩略图：首次访问用 Pillow 生成并缓存到 media/thumbs/，之后直接回图。"""
    af = get_object_or_404(ArchiveFile.objects.select_related("archive"), pk=pk)
    if not af.is_image or not af.file:
        raise Http404
    thumb_dir = os.path.join(settings.MEDIA_ROOT, "thumbs")
    os.makedirs(thumb_dir, exist_ok=True)
    thumb_path = os.path.join(thumb_dir, f"af{af.pk}.jpg")
    if not os.path.exists(thumb_path):
        from PIL import Image
        with Image.open(af.file.path) as img:
            # 透明/调色板图先转 RGBA 白底，避免缩略图黑底
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


@login_required
@perm_required("archive", "delete")
def archive_delete(request, pk):
    arc = get_object_or_404(Archive, pk=pk)
    if request.method == "POST":
        _log(request, "删除", f"档案#{arc.id}", f"删除档案: {arc.code} - {arc.name}")
        arc.delete()
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": "档案已删除"})
        return redirect("archive:overview")
    return render(request, "archive/archive_confirm_delete.html", {"archive": arc})


# ---------- 二维码 / 操作日志 ----------

@login_required
@perm_required("archive", "read")
def qrcode(request, pk):
    """生成档案位置二维码图片（PNG）。"""
    try:
        import qrcode
    except ImportError:
        return HttpResponse("请先安装 qrcode 库：pip install qrcode[pil]", status=500)
    arc = get_object_or_404(Archive, pk=pk)
    # 二维码内容：档案编号 + 物理路径
    data = f"{arc.code}|{arc.location_path}|{arc.name}"
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    response = HttpResponse(buf.getvalue(), content_type="image/png")
    response["Content-Disposition"] = f'inline; filename="qrcode_{arc.code}.png"'
    return response


@login_required
@perm_required("archive", "read")
def log_list(request):
    # 后端分页（10/30/50）：替代原先硬截断最近 200 条，全部留痕均可翻页查看
    qs = OperationLog.objects.filter(module_type="档案管理").order_by("-operation_time")
    per_page = _per_page(request)
    page_obj = Paginator(qs, per_page).get_page(request.GET.get("page"))
    return render(request, "archive/log_list.html", {
        "logs": page_obj, "page_obj": page_obj, "per_page": per_page,
    })
