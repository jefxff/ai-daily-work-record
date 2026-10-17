"""档案管理模块视图。

功能：档案柜/档案/类别 CRUD、档案总览网格、详情、借阅、导入导出、二维码。
权限：基于 RBAC，模块 key=archive。验收前仅超管可见（registry 不授予内置角色）。
"""

import csv
import io
import json
import os
import re
from datetime import date, datetime, timedelta

from django.contrib import messages
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, HttpResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from urllib.parse import quote

from accounts.decorators import perm_required
from records.models import OperationLog
from records.views import _per_page

from .forms import (
    ArchiveForm, BorrowForm, CabinetForm, CategoryForm, LedgerForm,
    LicenseForm, AnnualReviewForm, LicenseBorrowForm, validate_archive_file,
)
from .models import (
    AnnualReview, Archive, ArchiveFile, BorrowRecord, Cabinet, Category,
    Ledger, License, LicenseBorrowRecord, _add_months,
)


# ---------- 辅助工具 ----------

def _operator(request) -> str:
    """获取操作人展示名。"""
    user = request.user
    profile = getattr(user, "profile", None)
    if profile and getattr(profile, "real_name", ""):
        return profile.real_name
    return user.username


def _log(request, op_type: str, obj_id: str, summary: str):
    """写入操作留痕（复用 records.OperationLog）。"""
    OperationLog.objects.create(
        operator=_operator(request),
        module_type="档案管理",
        operation_type=op_type,
        object_identifier=obj_id,
        change_summary=summary,
    )


def _is_ajax(request) -> bool:
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


# ---------- 档案总览（按台账列表） ----------

@login_required
@perm_required("archive", "read")
def overview(request):
    """档案总览页：顶部仪表盘 + 各台账（对应一份 Excel 台账）卡片列表。

    aq：档案搜索（名称/编号/相对方/合同号/部门/位置编码/标签/经办人/描述），
        命中台账内显示全部命中档案（上限 200 条防卡顿）。
    status=expiring / borrowed：按即将到期 / 借出中筛选台账内档案。
    category：按类别筛选台账内档案。
    """
    aq = request.GET.get("aq", "").strip()
    category_id = request.GET.get("category")
    status = request.GET.get("status")  # expiring / borrowed

    today = date.today()
    expire_threshold = today + timedelta(days=30)

    def _ledger_qs(ledger):
        """台账（None=未分配）内档案查询集，套用筛选条件。"""
        if ledger is None:
            qs = Archive.objects.filter(ledger__isnull=True)
        else:
            qs = ledger.archives.all()
        if category_id:
            qs = qs.filter(category_id=category_id)
        if status == "expiring":
            qs = qs.filter(expire_date__isnull=False, expire_date__gte=today, expire_date__lte=expire_threshold)
        elif status == "borrowed":
            # 反向外键 join 陷阱：borrow_records__return_time__isnull=True 会把
            # 无任何借阅记录的档案也匹配进来（LEFT JOIN 全 NULL 行），必须用子查询
            qs = qs.filter(
                pk__in=BorrowRecord.objects.filter(return_time__isnull=True).values("archive_id")
            )
        return qs.order_by("-id")

    # 档案搜索：按多字段匹配，命中行在台账卡片中高亮
    matched_ids = []
    if aq:
        matched_ids = list(
            Archive.objects.filter(
                Q(name__icontains=aq)
                | Q(code__icontains=aq)
                | Q(counterpart__icontains=aq)
                | Q(contract_no__icontains=aq)
                | Q(department__icontains=aq)
                | Q(location_code__icontains=aq)
                | Q(archive_label__icontains=aq)
                | Q(handler__icontains=aq)
                | Q(tags__icontains=aq)
                | Q(description__icontains=aq)
            ).values_list("id", flat=True)
        )

    filtering = bool(aq or status or category_id)
    ledger_data = []
    for ledger in Ledger.objects.all():
        qs = _ledger_qs(ledger)
        total = qs.count()
        if aq:
            rows = list(qs.filter(id__in=matched_ids)[:200])
        else:
            rows = list(qs[:10])
        # 筛选状态下跳过无命中台账，避免满屏空卡片
        if filtering and not rows:
            continue
        ledger_data.append({
            "ledger": ledger,
            "rows": rows,
            "total": total,
            "expiring_count": ledger.archives.filter(
                expire_date__isnull=False, expire_date__gte=today, expire_date__lte=expire_threshold
            ).count(),
            "borrowed_count": ledger.archives.filter(
                pk__in=BorrowRecord.objects.filter(return_time__isnull=True).values("archive_id")
            ).count(),
        })

    # 未分配台账的档案（历史数据/手工录入未选台账）
    unassigned_qs = _ledger_qs(None)
    unassigned_total = unassigned_qs.count()
    unassigned_rows = []
    if unassigned_total:
        if aq:
            unassigned_rows = list(unassigned_qs.filter(id__in=matched_ids)[:200])
        else:
            unassigned_rows = list(unassigned_qs[:10])
    if filtering and not unassigned_rows:
        unassigned_total = 0

    # 仪表盘指标：台账总数 / 档案总数 / 已借出 / 逾期未还 / 即将到期
    total_ledgers = Ledger.objects.count()
    total_archives = Archive.objects.count()
    borrowed_count = BorrowRecord.objects.filter(return_time__isnull=True).count()
    overdue_count = BorrowRecord.objects.filter(return_time__isnull=True, due_time__lt=today).count()
    expiring_count = Archive.objects.filter(
        expire_date__isnull=False, expire_date__lte=expire_threshold, expire_date__gte=today
    ).count()

    categories = Category.objects.all()
    # 台账排序弹窗：完整台账列表（不受筛选影响），按当前显示顺序
    sort_ledgers = Ledger.objects.all()

    context = {
        "ledger_data": ledger_data,
        "sort_ledgers": sort_ledgers,
        "unassigned_rows": unassigned_rows,
        "unassigned_total": unassigned_total,
        "categories": categories,
        "category_id": category_id,
        "status": status,
        "aq": aq,
        "matched_ids": matched_ids,
        "match_count": len(matched_ids),
        "total_ledgers": total_ledgers,
        "total_archives": total_archives,
        "borrowed_count": borrowed_count,
        "overdue_count": overdue_count,
        "expiring_count": expiring_count,
        "filtering": filtering,
    }
    return render(request, "archive/overview.html", context)


# ---------- 台账 CRUD / 详情 ----------

@login_required
@perm_required("archive", "read")
def ledger_detail(request, pk):
    """台账详情页：完整档案列表（分页 10/30/50，站内搜索）。"""
    ledger = get_object_or_404(Ledger, pk=pk)
    q = request.GET.get("q", "").strip()
    per_page = _per_page(request)

    qs = ledger.archives.order_by("-id")
    if q:
        qs = qs.filter(
            Q(name__icontains=q) | Q(code__icontains=q) | Q(counterpart__icontains=q)
            | Q(contract_no__icontains=q) | Q(department__icontains=q)
            | Q(archive_label__icontains=q) | Q(handler__icontains=q) | Q(tags__icontains=q)
        )
    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(request.GET.get("page"))

    today = date.today()
    expiring_count = ledger.archives.filter(
        expire_date__isnull=False, expire_date__lte=today + timedelta(days=30), expire_date__gte=today
    ).count()
    borrowed_count = ledger.archives.filter(
        pk__in=BorrowRecord.objects.filter(return_time__isnull=True).values("archive_id")
    ).count()

    return render(request, "archive/ledger_detail.html", {
        "ledger": ledger, "page_obj": page_obj, "q": q, "per_page": per_page,
        "expiring_count": expiring_count, "borrowed_count": borrowed_count,
        "archive_count": ledger.archives.count(),
    })


@login_required
@perm_required("archive", "create")
def ledger_create(request):
    form = LedgerForm(request.POST or None)
    if form.is_valid():
        lg = form.save()
        _log(request, "新增", f"台账#{lg.id}", f"新增台账: {lg.name}")
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": f"台账 {lg.name} 已创建"})
        return redirect("archive:overview")
    if _is_ajax(request) and request.method == "POST":
        html = render(request, "archive/_ledger_form_modal.html", {"form": form, "mode": "create"}).content.decode()
        return JsonResponse({"ok": False, "html": html}, status=422)
    if _is_ajax(request):
        html = render(request, "archive/_ledger_form_modal.html", {"form": form, "mode": "create"}).content.decode()
        return JsonResponse({"ok": True, "html": html})
    return redirect("archive:overview")


@login_required
@perm_required("archive", "update")
def ledger_edit(request, pk):
    lg = get_object_or_404(Ledger, pk=pk)
    form = LedgerForm(request.POST or None, instance=lg)
    if form.is_valid():
        lg = form.save()
        _log(request, "编辑", f"台账#{lg.id}", f"编辑台账: {lg.name}")
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": "台账已更新"})
        return redirect("archive:overview")
    if _is_ajax(request) and request.method == "POST":
        html = render(request, "archive/_ledger_form_modal.html", {"form": form, "mode": "edit", "ledger": lg}).content.decode()
        return JsonResponse({"ok": False, "html": html}, status=422)
    if _is_ajax(request):
        html = render(request, "archive/_ledger_form_modal.html", {"form": form, "mode": "edit", "ledger": lg}).content.decode()
        return JsonResponse({"ok": True, "html": html})
    return redirect("archive:overview")


@login_required
@perm_required("archive", "update")
def ledger_move(request, pk):
    """台账排序：置顶 / 上移 / 下移（direction=top/up/down）。

    历史数据 sort_order 可能全部为 0（仅按 id 排），单纯交换两行字段值会无效，
    因此每次调整后按新顺序整体重排 sort_order（0..N-1）。
    """
    ledger = get_object_or_404(Ledger, pk=pk)
    direction = request.POST.get("direction", "up")
    if request.method == "POST" and direction in ("top", "up", "down"):
        ledgers = list(Ledger.objects.order_by("sort_order", "id"))
        idx = next((i for i, l in enumerate(ledgers) if l.pk == ledger.pk), None)
        if idx is not None:
            if direction == "top":
                j = 0
            elif direction == "up":
                j = max(idx - 1, 0)
            else:
                j = min(idx + 1, len(ledgers) - 1)
            if j != idx:
                item = ledgers.pop(idx)
                ledgers.insert(j, item)
                for i, l in enumerate(ledgers):
                    if l.sort_order != i:
                        l.sort_order = i
                        l.save(update_fields=["sort_order"])
                _log(request, "编辑", f"台账#{ledger.id}", f"调整台账排序: {ledger.name}（{direction}）")
    if _is_ajax(request):
        return JsonResponse({"ok": True})
    return redirect("archive:overview")


@login_required
@perm_required("archive", "delete")
def ledger_delete(request, pk):
    lg = get_object_or_404(Ledger, pk=pk)
    if request.method == "POST":
        cnt = lg.archives.count()
        name = lg.name
        _log(request, "删除", f"台账#{lg.id}", f"删除台账: {name}（级联删除档案 {cnt} 份）")
        lg.archives.all().delete()  # 级联删除台账下全部档案（附件、借出记录随之删除）
        lg.delete()
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": f"台账已删除（含档案 {cnt} 份）"})
        messages.success(request, f"台账 {name} 已删除（含档案 {cnt} 份）")
        return redirect("archive:overview")
    return render(request, "archive/ledger_confirm_delete.html", {
        "ledger": lg, "archive_count": lg.archives.count(),
    })


# ---------- 档案柜 CRUD ----------

@login_required
@perm_required("archive", "read")
def cabinet_list(request):
    """档案柜列表：编号、位置、各层格数与空位占用一览。"""
    cabinets = Cabinet.objects.prefetch_related("archives", "layer_configs")
    for cab in cabinets:
        cab.archive_count = cab.archives.count()
    return render(request, "archive/cabinet_list.html", {"cabinets": cabinets})


def _parse_layer_cells(post, layers, instance=None):
    """从 POST 解析各层格数 layer_cell_1..N；缺失的层回退现有层记录，再回退默认 10。"""
    existing = {}
    if instance is not None and instance.pk:
        existing = {r.layer_no: r.cells for r in instance.layer_configs.all()}
    return [post.get(f"layer_cell_{i}") or existing.get(i, 10) for i in range(1, layers + 1)]


def _cabinet_modal_ctx(form, mode, cabinet=None):
    """弹窗/整页柜子表单渲染 context：带上现有各层格数供 JS 预填。"""
    cells = []
    if cabinet is not None and cabinet.pk:
        cells = list(cabinet.layer_configs.order_by("layer_no").values_list("cells", flat=True))
    ctx = {"form": form, "mode": mode, "layer_cells_json": json.dumps(cells)}
    if cabinet is not None:
        ctx["cabinet"] = cabinet
    return ctx


def _save_cabinet(request, form, mode):
    """保存柜子并在同一事务内同步层记录；层配置非法时把错误挂到表单。返回是否成功。"""
    try:
        with transaction.atomic():
            cab = form.save()
            cab.sync_layers(_parse_layer_cells(request.POST, cab.layers, cab))
    except ValueError as e:
        form.add_error(None, str(e))
        return None
    _log(request, "新增" if mode == "create" else "编辑", f"档案柜#{cab.id}",
         f"{'新增' if mode == 'create' else '编辑'}档案柜: {cab.code}（{cab.location}），各层格数 {cab.layer_cells_summary()}")
    return cab


@login_required
@perm_required("archive", "create")
def cabinet_create(request):
    form = CabinetForm(request.POST or None)
    if form.is_valid():
        cab = _save_cabinet(request, form, "create")
        if cab:
            if _is_ajax(request):
                return JsonResponse({"ok": True, "message": f"档案柜 {cab.code} 已创建"})
            return redirect("archive:overview")
    if _is_ajax(request) and request.method == "POST":
        html = render(request, "archive/_cabinet_form_modal.html", _cabinet_modal_ctx(form, "create")).content.decode()
        return JsonResponse({"ok": False, "html": html}, status=422)
    if _is_ajax(request):
        html = render(request, "archive/_cabinet_form_modal.html", _cabinet_modal_ctx(form, "create")).content.decode()
        return JsonResponse({"ok": True, "html": html})
    return render(request, "archive/cabinet_form.html", _cabinet_modal_ctx(form, "create"))


@login_required
@perm_required("archive", "update")
def cabinet_edit(request, pk):
    cab = get_object_or_404(Cabinet, pk=pk)
    form = CabinetForm(request.POST or None, instance=cab)
    if form.is_valid():
        saved = _save_cabinet(request, form, "edit")
        if saved:
            if _is_ajax(request):
                return JsonResponse({"ok": True, "message": "档案柜已更新"})
            return redirect("archive:overview")
    if _is_ajax(request) and request.method == "POST":
        html = render(request, "archive/_cabinet_form_modal.html", _cabinet_modal_ctx(form, "edit", cab)).content.decode()
        return JsonResponse({"ok": False, "html": html}, status=422)
    if _is_ajax(request):
        html = render(request, "archive/_cabinet_form_modal.html", _cabinet_modal_ctx(form, "edit", cab)).content.decode()
        return JsonResponse({"ok": True, "html": html})
    return render(request, "archive/cabinet_form.html", _cabinet_modal_ctx(form, "edit", cab))


@login_required
@perm_required("archive", "delete")
def cabinet_delete(request, pk):
    cab = get_object_or_404(Cabinet, pk=pk)
    if cab.archives.exists():
        messages.error(request, f"档案柜 {cab.code} 下还有 {cab.archives.count()} 份档案，无法删除")
        return redirect("archive:overview")
    if request.method == "POST":
        _log(request, "删除", f"档案柜#{cab.id}", f"删除档案柜: {cab.code}")
        cab.delete()
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": "档案柜已删除"})
        return redirect("archive:overview")
    return render(request, "archive/cabinet_confirm_delete.html", {"cabinet": cab})


# ---------- 档案 CRUD ----------

@login_required
@perm_required("archive", "create")
def archive_create(request):
    form = ArchiveForm(request.POST or None, request.FILES or None)
    if form.is_valid():
        arc = form.save(commit=False)
        arc.creator = request.user
        arc.save()
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
        arc = form.save()
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
        arc = form.save()
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


# ---------- 类别管理 ----------

@login_required
@perm_required("archive", "read")
def category_list(request):
    categories = Category.objects.all().order_by("sort_order", "id")
    return render(request, "archive/category_list.html", {"categories": categories})


@login_required
@perm_required("archive", "create")
def category_create(request):
    form = CategoryForm(request.POST or None)
    if form.is_valid():
        cat = form.save()
        _log(request, "新增", f"类别#{cat.id}", f"新增类别: {cat.name}")
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": f"类别 {cat.name} 已创建"})
        return redirect("archive:category_list")
    if _is_ajax(request) and request.method == "POST":
        html = render(request, "archive/_category_form_modal.html", {"form": form, "mode": "create"}).content.decode()
        return JsonResponse({"ok": False, "html": html}, status=422)
    if _is_ajax(request):
        html = render(request, "archive/_category_form_modal.html", {"form": form, "mode": "create"}).content.decode()
        return JsonResponse({"ok": True, "html": html})
    return render(request, "archive/category_form.html", {"form": form, "mode": "create"})


@login_required
@perm_required("archive", "update")
def category_edit(request, pk):
    cat = get_object_or_404(Category, pk=pk)
    form = CategoryForm(request.POST or None, instance=cat)
    if form.is_valid():
        cat = form.save()
        _log(request, "编辑", f"类别#{cat.id}", f"编辑类别: {cat.name}")
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": "类别已更新"})
        return redirect("archive:category_list")
    if _is_ajax(request) and request.method == "POST":
        html = render(request, "archive/_category_form_modal.html", {"form": form, "mode": "edit", "category": cat}).content.decode()
        return JsonResponse({"ok": False, "html": html}, status=422)
    if _is_ajax(request):
        html = render(request, "archive/_category_form_modal.html", {"form": form, "mode": "edit", "category": cat}).content.decode()
        return JsonResponse({"ok": True, "html": html})
    return render(request, "archive/category_form.html", {"form": form, "mode": "edit", "category": cat})


@login_required
@perm_required("archive", "delete")
def category_delete(request, pk):
    cat = get_object_or_404(Category, pk=pk)
    if cat.archives.exists():
        messages.error(request, f"类别 {cat.name} 下还有档案，无法删除")
        return redirect("archive:category_list")
    if request.method == "POST":
        _log(request, "删除", f"类别#{cat.id}", f"删除类别: {cat.name}")
        cat.delete()
        return redirect("archive:category_list")
    return render(request, "archive/category_confirm_delete.html", {"category": cat})


# ---------- 借阅管理 ----------

@login_required
@perm_required("archive", "read")
def borrow_list(request):
    status = request.GET.get("status")
    qs = BorrowRecord.objects.select_related("archive")
    if status:
        # 刷新逾期状态
        for r in qs.filter(return_time__isnull=True):
            r.refresh_status()
            r.save(update_fields=["status"])
        qs = qs.filter(status=status)
    return render(request, "archive/borrow_list.html", {"records": qs, "status": status})


@login_required
@perm_required("archive", "create")
def borrow_create(request, archive_pk):
    arc = get_object_or_404(Archive, pk=archive_pk)
    if arc.is_borrowed:
        messages.error(request, f"档案 {arc.code} 已借出，无法再次借阅")
        if _is_ajax(request):
            return JsonResponse({"ok": False, "message": f"档案 {arc.code} 已借出，无法再次借阅"}, status=422)
        return redirect("archive:archive_detail", pk=arc.pk)
    form = BorrowForm(request.POST or None)
    if form.is_valid():
        rec = form.save(commit=False)
        rec.archive = arc
        rec.save()
        _log(request, "借阅", f"档案#{arc.id}", f"借出档案: {arc.code} 给 {rec.borrower}，应还 {rec.due_time}")
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": "借阅登记成功"})
        return redirect("archive:archive_detail", pk=arc.pk)
    if _is_ajax(request):
        html = render_to_string(
            "archive/_borrow_form_modal.html", {"form": form, "archive": arc}, request=request
        )
        # 打开弹窗（GET）返回 ok:true + html；提交校验失败（POST）返回 422 重新注入表单
        if request.method == "POST":
            return JsonResponse({"ok": False, "html": html}, status=422)
        return JsonResponse({"ok": True, "html": html})
    return render(request, "archive/borrow_form.html", {"form": form, "archive": arc})


@login_required
@perm_required("archive", "update")
def borrow_return(request, pk):
    rec = get_object_or_404(BorrowRecord, pk=pk)
    if request.method == "POST":
        from django.utils import timezone
        rec.return_time = timezone.now()
        rec.refresh_status()
        rec.save()
        _log(request, "归还", f"档案#{rec.archive_id}", f"归还档案: {rec.archive.code}")
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": "归还成功"})
        return redirect("archive:borrow_list")
    return render(request, "archive/borrow_confirm_return.html", {"record": rec})


# ---------- 导入 / 导出 ----------

def _read_import_rows(f):
    """解析上传的导入文件，返回字典行列表。支持 .xlsx / .xls / .csv。"""
    name = (getattr(f, "name", "") or "").lower()
    if name.endswith(".csv"):
        raw = f.read()
        content = None
        for enc in ("utf-8-sig", "gbk"):
            try:
                content = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if content is None:
            raise ValueError("文件编码无法识别，请使用 UTF-8 编码 CSV")
        return list(csv.DictReader(io.StringIO(content)))
    if name.endswith(".xls"):
        import xlrd
        wb = xlrd.open_workbook(file_contents=f.read())
        ws = wb.sheet_by_index(0)
        if ws.nrows < 2:
            return []
        headers = [str(ws.cell_value(0, c) or "").strip() for c in range(ws.ncols)]
        out = []
        for r in range(1, ws.nrows):
            row = {}
            for c in range(ws.ncols):
                if c < len(headers) and headers[c]:
                    row[headers[c]] = str(ws.cell_value(r, c) or "").strip()
            if any(v for v in row.values()):
                out.append(row)
        return out
    # .xlsx
    from openpyxl import load_workbook
    wb = load_workbook(f, data_only=True)
    ws = wb["档案数据"] if "档案数据" in wb.sheetnames else wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(h).strip() if h is not None else "" for h in rows[0]]
    out = []
    for r in rows[1:]:
        if all(v is None or str(v).strip() == "" for v in r):
            continue
        out.append({h: ("" if v is None else str(v).strip()) for h, v in zip(headers, r)})
    return out


# 智能导入引擎：字段映射表
FIELD_CHOICES = [
    ("code", "档案编号"),
    ("name", "档案名称/标题"),
    ("counterpart", "公司名称/相对方"),
    ("contract_no", "合同编号"),
    ("amount", "费用/金额"),
    ("sign_date", "签订日期"),
    ("expire_date", "截止/到期日期"),
    ("department", "部门"),
    ("security_level", "保密等级"),
    ("archive_label", "档案标签"),
    ("location_code", "位置编码"),
    ("handler", "移交人/经办人"),
    ("copies", "份数"),
    ("attachment_desc", "附件"),
    ("description", "备注"),
    ("tags", "标签"),
]


def _parse_excel_sheets(f):
    """解析 Excel 文件所有 sheet，返回 {sheet_name: {headers: [], rows: [{col: val}]}}。"""
    name = (getattr(f, "name", "") or "").lower()
    result = {}
    if name.endswith(".xls"):
        import xlrd
        wb = xlrd.open_workbook(file_contents=f.read())
        for idx in range(wb.nsheets):
            ws = wb.sheet_by_index(idx)
            if ws.nrows < 2:
                continue
            headers = [str(ws.cell_value(0, c) or "").strip() for c in range(ws.ncols)]
            rows = []
            for r in range(1, ws.nrows):
                row = {}
                for c in range(ws.ncols):
                    if c < len(headers) and headers[c]:
                        row[headers[c]] = str(ws.cell_value(r, c) or "").strip()
                if any(row.values()):
                    rows.append(row)
            if rows:
                result[ws.name] = {"headers": [h for h in headers if h], "rows": rows}
        return result
    from openpyxl import load_workbook
    wb = load_workbook(f, data_only=True)
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if len(rows) < 2:
            continue
        headers = [str(h).strip() if h is not None else "" for h in rows[0]]
        data_rows = []
        for r in rows[1:]:
            row = {}
            for h, v in zip(headers, r):
                if h:
                    row[h] = ("" if v is None else str(v).strip())
            if any(row.values()):
                data_rows.append(row)
        if data_rows:
            result[ws.title] = {"headers": [h for h in headers if h], "rows": data_rows}
    return result


def _auto_map_columns(headers):
    """根据表头文本自动推断系统字段映射。返回 {col_name: field_name}。"""
    mapping = {}
    keyword_map = {
        "code": ["档案编号", "编号", "序列", "序号", "序 号"],
        "name": ["标题", "名称", "档案名称", "文件名称", "合同名称"],
        "counterpart": ["公司名称", "相对方", "相对人", "供应商", "合作方", "发文单位", "签订单位", "名称/姓名", "单位", "公司名称/相对人"],
        "contract_no": ["合同编号", "单号", "编号", "文书编号", "文件编号", "报告编号"],
        "amount": ["费用", "金额", "额度", "保费"],
        "sign_date": ["签订日期", "签发日期", "公证日期", "申请日期", "日期", "签订时间"],
        "expire_date": ["到期日", "截止日期", "截止日期", "到期时间", "截止时间"],
        "department": ["部门"],
        "security_level": ["保密等级"],
        "archive_label": ["档案名称", "档案标签"],
        "location_code": ["位置", "归位处"],
        "handler": ["移交人", "经办人", "经手人", "移送人"],
        "copies": ["份数"],
        "attachment_desc": ["附件"],
        "description": ["备注"],
        "tags": ["标签"],
    }
    for col in headers:
        for field, keywords in keyword_map.items():
            if col.strip() in keywords and field not in mapping.values():
                mapping[col] = field
                break
    return mapping


@login_required
@perm_required("archive", "create")
def import_template(request):
    """下载批量导入模板（Excel，含填写说明与示例数据）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()

    # 说明页
    ws = wb.active
    ws.title = "填写说明"
    instructions = [
        ["档案批量导入模板 - 填写说明"],
        [""],
        ["一、基本要求"],
        ["1. 请在「档案数据」工作表中填写档案信息，不要修改表头名称和列的顺序。"],
        ["2. 标有 * 的列为必填项：编号、名称、档案柜、层、格；其余列可留空。"],
        ["3. 每行填写一份档案；填写完成后保存为 .xlsx 文件上传导入（也支持 CSV）。"],
        ["4. 示例行仅供参考，正式导入前请删除示例行。"],
        [""],
        ["二、各列填写说明"],
        ["编号 *：档案唯一编号，不能与系统中已有编号重复，如 ARC2025-001。"],
        ["名称 *：档案名称，如 2025年度客户客诉记录。"],
        ["档案柜 *：档案柜编号，必须已存在于系统中（如 A01），可在档案总览页查看。"],
        ["层 *：存放层号，正整数，不能超过该档案柜设置的层数（如 4 层柜只能填 1-4）。"],
        ["格 *：存放格号，正整数，不能超过该柜每层格数；同一位置只能存放一份档案。"],
        ["类别：类别名称，需与系统中类别名称完全一致，可留空。"],
        ["标签：多个标签用逗号分隔，如 重要,2025。"],
        ["描述：档案补充描述，可留空。"],
        [""],
        ["三、常见导入失败原因"],
        ["- 档案柜编号不存在：请先在系统「档案总览」中创建对应档案柜。"],
        ["- 位置已被占用：同一柜、同层、同格已有档案，请更换层号或格号。"],
        ["- 编号重复：档案编号在系统中必须唯一，不能与已有档案重复。"],
        ["- 层/格超出范围：填写的层号或格号超过了档案柜的容量设置。"],
    ]
    for row in instructions:
        ws.append(row)
    ws.column_dimensions["A"].width = 100
    ws["A1"].font = Font(bold=True, size=14)
    for r in ws.iter_rows(min_row=3):
        for c in r:
            c.alignment = Alignment(wrap_text=True, vertical="top")

    # 数据页
    ws2 = wb.create_sheet("档案数据")
    ws2.append(["编号", "名称", "档案柜", "层", "格", "类别", "标签", "描述"])
    ws2.append(["ARC2025-001", "示例档案一（导入前请删除本行）", "A01", 1, 1, "客户档案", "重要,2025", "示例描述，可留空"])
    ws2.append(["ARC2025-002", "示例档案二（导入前请删除本行）", "A01", 1, 2, "", "样例", ""])
    for cell in ws2[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="E6F7FF")
    for i, w in enumerate([16, 34, 12, 8, 8, 16, 20, 40], start=1):
        ws2.column_dimensions[get_column_letter(i)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    resp = HttpResponse(
        buf.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = 'attachment; filename="archive_import_template.xlsx"'
    _log(request, "下载", "导入模板", "下载档案批量导入模板")
    return resp


@login_required
@perm_required("archive", "create")
def import_archives(request):
    """智能列映射导入引擎：上传→列映射→执行导入。"""
    if request.method == "GET":
        return render(request, "archive/import_step1.html", {})

    # ---- Step 1: 上传文件 → 解析 sheet → 返回列映射页面 ----
    if request.POST.get("step") == "upload":
        f = request.FILES.get("file")
        if not f:
            messages.error(request, "请选择 Excel 文件（.xlsx / .xls / .csv）")
            return render(request, "archive/import_step1.html", {})
        try:
            sheets = _parse_excel_sheets(f)
        except Exception as e:
            messages.error(request, f"文件解析失败：{e}")
            return render(request, "archive/import_step1.html", {})
        if not sheets:
            messages.error(request, "未读取到有效数据（可能 sheet 为空或表头不在第 1 行）")
            return render(request, "archive/import_step1.html", {})
        # 自动推断映射，并为每个 header 预构建展示数据
        # （避免模板用变量做字典 key 的限制：Django 模板 . 访问只接受字面 key）
        for sname, sdata in sheets.items():
            auto_map = _auto_map_columns(sdata["headers"])
            sdata["auto_mapping"] = auto_map
            first_row = sdata["rows"][0] if sdata["rows"] else {}
            sdata["header_choices"] = [
                {
                    "col": col,
                    "auto": auto_map.get(col, ""),
                    "sample": first_row.get(col, ""),
                }
                for col in sdata["headers"]
            ]
        request.session["import_sheets"] = sheets
        # 台账名称默认 = 上传文件名去扩展名（一个 Excel 对应一个台账）
        default_ledger = os.path.splitext(getattr(f, "name", "") or "")[0]
        request.session["import_ledger_name"] = default_ledger
        return render(request, "archive/import_step2.html", {
            "sheets": sheets, "field_choices": FIELD_CHOICES, "default_ledger": default_ledger,
        })

    # ---- Step 2: 执行导入 ----
    if request.POST.get("step") == "import":
        sheets = request.session.get("import_sheets", {})
        if not sheets:
            messages.error(request, "会话已过期，请重新上传文件")
            return render(request, "archive/import_step1.html", {})
        # 台账归属：Step2 表单填写的台账名称（默认文件名去扩展名）
        ledger = None
        ledger_name = (request.POST.get("ledger_name") or request.session.get("import_ledger_name", "") or "").strip()
        if ledger_name:
            ledger, _ = Ledger.objects.get_or_create(name=ledger_name[:200])
        total_created = 0
        total_errors = []
        for sname, sdata in sheets.items():
            mapping = {}
            for col in sdata["headers"]:
                val = request.POST.get(f"map__{sname}__{col}", "")
                if val:
                    mapping[col] = val
            if not mapping.get("__has_name__") and not any(v == "name" for v in mapping.values()):
                total_errors.append(f"[{sname}] 未映射「档案名称/标题」列，跳过")
                continue
            for i, row in enumerate(sdata["rows"], start=2):
                try:
                    obj = {"creator": request.user}
                    for col, val in row.items():
                        field = mapping.get(col)
                        if field:
                            raw_val = val.strip()
                            if field in ("code",) and not raw_val:
                                continue
                            if field in ("sign_date", "expire_date") and raw_val:
                                from datetime import datetime as _dt
                                try:
                                    if " " in raw_val:
                                        raw_val = raw_val.split(" ")[0]
                                    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%m/%d/%Y"):
                                        try:
                                            obj[field] = _dt.strptime(raw_val, fmt).date()
                                            break
                                        except ValueError:
                                            continue
                                except Exception:
                                    pass
                            elif field == "copies" and raw_val:
                                try:
                                    obj[field] = int(float(raw_val))
                                except ValueError:
                                    pass
                            else:
                                obj[field] = raw_val[:200] if field != "description" else raw_val
                    if not obj.get("name"):
                        continue
                    if not obj.get("code"):
                        import random, string
                        obj["code"] = "IMP" + "".join(random.choices(string.digits, k=8))
                    if Archive.objects.filter(code=obj["code"]).exists():
                        obj["code"] = obj["code"] + "D"
                    if ledger:
                        obj["ledger"] = ledger
                    obj["source_sheet"] = sname[:100]
                    Archive.objects.create(**obj)
                    total_created += 1
                except Exception as e:
                    total_errors.append(f"[{sname}]第{i}行：{e}")
        _log(request, "导入", "档案批量导入", f"成功 {total_created} 条，失败 {len(total_errors)} 条")
        if total_created:
            messages.success(request, f"成功导入 {total_created} 条档案")
        for err in total_errors[:10]:
            messages.warning(request, err)
        if len(total_errors) > 10:
            messages.warning(request, f"……另有 {len(total_errors) - 10} 条错误未展示")
        del request.session["import_sheets"]
        request.session.pop("import_ledger_name", None)
        return redirect("archive:overview")

    return redirect("archive:overview")


@login_required
@perm_required("archive", "read")
def export_archives(request):
    response = HttpResponse(content_type="text/csv; charset=utf-8-sig")
    response["Content-Disposition"] = f'attachment; filename="archives_{datetime.now():%Y%m%d_%H%M}.csv"'
    writer = csv.writer(response)
    writer.writerow(["所属台账", "来源Sheet", "编号", "名称", "档案柜", "位置", "层", "格", "类别", "标签", "创建人", "创建时间"])
    for a in Archive.objects.select_related("cabinet", "category", "creator", "ledger"):
        writer.writerow([
            a.ledger.name if a.ledger else "", a.source_sheet,
            a.code, a.name,
            a.cabinet.code if a.cabinet_id else "", a.cabinet.location if a.cabinet_id else "",
            a.layer if a.layer is not None else "", a.cell if a.cell is not None else "",
            a.category.name if a.category else "", a.tags,
            a.creator.username if a.creator else "",
            a.created_at.strftime("%Y-%m-%d %H:%M"),
        ])
    _log(request, "导出", "档案清单导出", "导出全部档案清单")
    return response


# ---------- 表级通用导入 / 导出（导出 → Excel 修改 → 导入更新 闭环） ----------

def _parse_date_val(raw):
    """多格式日期解析，失败返回 None。"""
    from datetime import datetime as _dt
    raw = raw.split(" ")[0] if " " in raw else raw
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%m/%d/%Y"):
        try:
            return _dt.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_datetime_val(raw):
    """多格式日期时间解析，失败返回 None（naive 值转为当前时区 aware）。"""
    from datetime import datetime as _dt
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            v = _dt.strptime(raw, fmt)
            if timezone.is_naive(v):
                v = timezone.make_aware(v)
            return v
        except ValueError:
            continue
    return None


def _fmt_date(v):
    return v.strftime("%Y-%m-%d") if v else ""


def _fmt_datetime(v):
    if not v:
        return ""
    if timezone.is_aware(v):
        v = timezone.localtime(v)
    return v.strftime("%Y-%m-%d %H:%M")


def _category_from_path(path):
    """按「A/B/C」全路径逐级 get_or_create 类别，返回末级节点；空路径返回 None。"""
    node = None
    for part in [p.strip() for p in path.split("/") if p.strip()]:
        node, _ = Category.objects.get_or_create(name=part[:100], parent=node)
    return node


def _col(header, getter, setter=None):
    """列定义：(表头, 取值函数, 写值函数)。setter 为 None 表示该列仅导出展示。"""
    return (header, getter, setter)


def _setter_int(field):
    def _s(obj, raw):
        try:
            setattr(obj, field, int(float(raw)))
        except (TypeError, ValueError):
            raise ValueError(f"{field} 需为数字，收到「{raw}」")
    return _s


def _setter_date(field):
    def _s(obj, raw):
        v = _parse_date_val(raw)
        if v is None:
            raise ValueError(f"日期格式无法识别：{raw}（支持 2026-01-31 / 2026/1/31）")
        setattr(obj, field, v)
    return _s


def _setter_datetime(field):
    def _s(obj, raw):
        v = _parse_datetime_val(raw)
        if v is None:
            raise ValueError(f"时间格式无法识别：{raw}")
        setattr(obj, field, v)
    return _s


def _setter_text(field, max_len=200):
    def _s(obj, raw):
        setattr(obj, field, raw[:max_len] if field != "description" and field != "remark" else raw)
    return _s


def _setter_fk_ledger(obj, raw):
    obj.ledger, _ = Ledger.objects.get_or_create(name=raw[:200])


def _setter_fk_category(obj, raw):
    obj.category = _category_from_path(raw)


def _setter_fk_cabinet(obj, raw):
    cab = Cabinet.objects.filter(code=raw).first()
    if not cab:
        raise ValueError(f"档案柜「{raw}」不存在，请先创建")
    obj.cabinet = cab


def _setter_fk_archive(obj, raw):
    arc = Archive.objects.filter(code=raw).first()
    if not arc:
        raise ValueError(f"档案编号「{raw}」不存在")
    obj.archive = arc


def _setter_fk_license(obj, raw):
    lic = License.objects.filter(code=raw).first()
    if not lic:
        raise ValueError(f"证照编号「{raw}」不存在")
    obj.license = lic


def _setter_cabinet_layer_cells(obj, raw):
    """「各层格数」列 setter：逗号分隔数字（如 10,10,8,8），暂存到实例待保存后同步。"""
    parts = [p.strip() for p in str(raw).replace("，", ",").split(",") if p.strip()]
    if not parts:
        raise ValueError("各层格数不能为空，格式如 10,10,8,8")
    try:
        obj._pending_layer_cells = [int(float(p)) for p in parts]
    except (TypeError, ValueError):
        raise ValueError(f"各层格数格式无法识别：{raw}（应为逗号分隔数字，如 10,10,8,8）")


def _finalize_cabinet(obj):
    """Cabinet 保存后同步层记录（导入引擎 / 表单路径共用）。"""
    if isinstance(obj, Cabinet):
        obj.sync_layers(getattr(obj, "_pending_layer_cells", None))
        if hasattr(obj, "_pending_layer_cells"):
            del obj._pending_layer_cells


TABLE_SPECS = {
    "archives": {
        "label": "档案",
        "model": Archive,
        "match_field": "code",
        "match_header": "编号",
        "redirect": "archive:overview",
        "queryset": lambda: Archive.objects.select_related("cabinet", "category", "ledger").order_by("id"),
        "columns": [
            _col("编号", lambda a: a.code),
            _col("名称", lambda a: a.name, _setter_text("name", 200)),
            _col("所属台账", lambda a: a.ledger.name if a.ledger_id else "", _setter_fk_ledger),
            _col("类别", lambda a: a.category.full_path if a.category_id else "", _setter_fk_category),
            _col("档案柜", lambda a: a.cabinet.code if a.cabinet_id else "", _setter_fk_cabinet),
            _col("层", lambda a: a.layer if a.layer is not None else "", _setter_int("layer")),
            _col("格", lambda a: a.cell if a.cell is not None else "", _setter_int("cell")),
            _col("位置编码", lambda a: a.location_code, _setter_text("location_code", 50)),
            _col("来源Sheet", lambda a: a.source_sheet, _setter_text("source_sheet", 100)),
            _col("公司名称/相对方", lambda a: a.counterpart, _setter_text("counterpart")),
            _col("合同编号", lambda a: a.contract_no, _setter_text("contract_no", 100)),
            _col("金额", lambda a: a.amount, _setter_text("amount", 50)),
            _col("签订日期", lambda a: _fmt_date(a.sign_date), _setter_date("sign_date")),
            _col("到期日期", lambda a: _fmt_date(a.expire_date), _setter_date("expire_date")),
            _col("部门", lambda a: a.department, _setter_text("department", 50)),
            _col("保密等级", lambda a: a.security_level, _setter_text("security_level", 20)),
            _col("档案标签", lambda a: a.archive_label, _setter_text("archive_label", 100)),
            _col("经办人", lambda a: a.handler, _setter_text("handler", 50)),
            _col("份数", lambda a: a.copies if a.copies is not None else "", _setter_int("copies")),
            _col("附件描述", lambda a: a.attachment_desc, _setter_text("attachment_desc")),
            _col("标签", lambda a: a.tags, _setter_text("tags")),
            _col("档案描述", lambda a: a.description, _setter_text("description")),
        ],
    },
    "ledgers": {
        "label": "台账",
        "model": Ledger,
        "match_field": "name",
        "match_header": "台账名称",
        "redirect": "archive:overview",
        "queryset": lambda: Ledger.objects.order_by("id"),
        "columns": [
            _col("台账名称", lambda l: l.name),
            _col("说明", lambda l: l.description, _setter_text("description", 300)),
            _col("排序", lambda l: l.sort_order, _setter_int("sort_order")),
        ],
    },
    "cabinets": {
        "label": "档案柜",
        "model": Cabinet,
        "match_field": "code",
        "match_header": "档案柜编号",
        "redirect": "archive:overview",
        "queryset": lambda: Cabinet.objects.prefetch_related("layer_configs").order_by("id"),
        "columns": [
            _col("档案柜编号", lambda c: c.code),
            _col("所在位置", lambda c: c.location, _setter_text("location")),
            _col("层数", lambda c: c.layers, _setter_int("layers")),
            _col("各层格数", lambda c: c.layer_cells_summary(), _setter_cabinet_layer_cells),
        ],
    },
    "categories": {
        "label": "档案类别",
        "model": Category,
        "match_field": "path",
        "match_header": "类别全路径",
        "redirect": "archive:category_list",
        "queryset": lambda: Category.objects.order_by("id"),
        "columns": [
            _col("类别全路径", lambda c: c.full_path),
            _col("排序", lambda c: c.sort_order, _setter_int("sort_order")),
        ],
    },
    "borrows": {
        "label": "借阅记录",
        "model": BorrowRecord,
        "match_field": "id",
        "match_header": "记录ID",
        "redirect": "archive:borrow_list",
        "queryset": lambda: BorrowRecord.objects.select_related("archive").order_by("id"),
        "columns": [
            _col("记录ID", lambda r: r.pk),
            _col("档案编号", lambda r: r.archive.code, _setter_fk_archive),
            _col("借阅人", lambda r: r.borrower, _setter_text("borrower", 50)),
            _col("借阅时间", lambda r: _fmt_datetime(r.borrow_time), _setter_datetime("borrow_time")),
            _col("应归还日期", lambda r: _fmt_date(r.due_time), _setter_date("due_time")),
            _col("实际归还时间", lambda r: _fmt_datetime(r.return_time), _setter_datetime("return_time")),
            _col("状态", lambda r: r.get_status_display()),
            _col("备注", lambda r: r.remark, _setter_text("remark")),
        ],
    },
    "licenses": {
        "label": "证照",
        "model": License,
        "match_field": "code",
        "match_header": "证照编号",
        "redirect": "archive:license_list",
        "queryset": lambda: License.objects.order_by("id"),
        "columns": [
            _col("证照编号", lambda l: l.code),
            _col("证照名称", lambda l: l.name, _setter_text("name")),
            _col("持证单位", lambda l: l.holder, _setter_text("holder")),
            _col("发证机关", lambda l: l.issuer, _setter_text("issuer")),
            _col("发证日期", lambda l: _fmt_date(l.issue_date), _setter_date("issue_date")),
            _col("有效期至", lambda l: _fmt_date(l.expire_date), _setter_date("expire_date")),
            _col("年审周期(月)", lambda l: l.review_cycle, _setter_int("review_cycle")),
            _col("下次年审日", lambda l: _fmt_date(l.next_review_date), _setter_date("next_review_date")),
            _col("状态", lambda l: l.status_display),
            _col("备注", lambda l: l.remark, _setter_text("remark")),
        ],
    },
    "reviews": {
        "label": "年审记录",
        "model": AnnualReview,
        "match_field": "id",
        "match_header": "记录ID",
        "redirect": "archive:license_list",
        "queryset": lambda: AnnualReview.objects.select_related("license").order_by("id"),
        "columns": [
            _col("记录ID", lambda r: r.pk),
            _col("证照编号", lambda r: r.license.code, _setter_fk_license),
            _col("年审日期", lambda r: _fmt_date(r.review_date), _setter_date("review_date")),
            _col("年审结果", lambda r: r.get_result_display(), None),
            _col("经办人", lambda r: r.reviewer, _setter_text("reviewer", 50)),
            _col("新有效期至", lambda r: _fmt_date(r.new_expire_date), _setter_date("new_expire_date")),
            _col("备注", lambda r: r.remark, _setter_text("remark", 500)),
        ],
    },
    "license_borrows": {
        "label": "证照借阅记录",
        "model": LicenseBorrowRecord,
        "match_field": "id",
        "match_header": "记录ID",
        "redirect": "archive:license_borrow_list",
        "queryset": lambda: LicenseBorrowRecord.objects.select_related("license").order_by("id"),
        "columns": [
            _col("记录ID", lambda r: r.pk),
            _col("证照编号", lambda r: r.license.code, _setter_fk_license),
            _col("借阅人", lambda r: r.borrower, _setter_text("borrower", 50)),
            _col("借阅时间", lambda r: _fmt_datetime(r.borrow_time), _setter_datetime("borrow_time")),
            _col("应归还日期", lambda r: _fmt_date(r.due_time), _setter_date("due_time")),
            _col("实际归还时间", lambda r: _fmt_datetime(r.return_time), _setter_datetime("return_time")),
            _col("状态", lambda r: r.get_status_display()),
            _col("备注", lambda r: r.remark, _setter_text("remark")),
        ],
    },
}

# 年审结果中文 ↔ 代码映射（导入解析用）
_REVIEW_RESULT_MAP = {v: k for k, v in dict(AnnualReview.RESULT_CHOICES).items()}


def _get_spec(table):
    spec = TABLE_SPECS.get(table)
    if not spec:
        raise Http404("未知的导入导出对象")
    return spec


def _match_obj(spec, key_raw):
    """按匹配键定位现有记录；未命中返回 None。"""
    if not key_raw:
        return None
    model = spec["model"]
    field = spec["match_field"]
    if field == "id":
        return model.objects.filter(pk=key_raw).first() if str(key_raw).isdigit() else None
    if field == "path":
        return None  # 类别按路径新建/定位，不走精确匹配
    return model.objects.filter(**{field: key_raw}).first()


def _apply_columns(spec, obj, row, is_created, field_errors=None):
    """按列定义把行数据写入对象；空值跳过（保留原值）。返回是否写入了任何字段。

    field_errors 传列表时启用列级容错：某列写值失败（如档案柜编号不存在、日期格式错误）
    则跳过该列保留原值，错误记入 [(列名, 原因)]；不传时保持原语义（抛异常回滚整行）。
    """
    changed = False
    for header, _getter, setter in spec["columns"]:
        if setter is None or header == spec["match_header"]:
            continue
        raw = str(row.get(header, "") or "").strip()
        if not raw:
            continue
        if spec["label"] == "年审记录" and header == "年审结果":
            code = _REVIEW_RESULT_MAP.get(raw)
            if not code:
                raise ValueError(f"年审结果需为：通过/未通过/待处理，收到「{raw}」")
            obj.result = code
            changed = True
            continue
        try:
            setter(obj, raw)
            changed = True
        except Exception as e:
            if field_errors is None:
                raise
            field_errors.append((header, str(e)))
    # 记录表新建时给默认借阅时间/状态，避免必填约束直接崩
    if is_created and hasattr(obj, "borrow_time") and obj.borrow_time is None:
        obj.borrow_time = timezone.now()
    return changed


def _validate_archive_row(row):
    """档案行只读预检（不写库），返回将忽略的单元格 [(列名, 原因)]。"""
    errs = []

    def raw(h):
        return str(row.get(h, "") or "").strip()

    cab = raw("档案柜")
    if cab and not Cabinet.objects.filter(code=cab).exists():
        names = "、".join(Cabinet.objects.values_list("code", flat=True)[:8]) or "暂无"
        errs.append(("档案柜", f"编号「{cab}」不存在（系统现有：{names}），该列将保留原值"))
    for h in ("层", "格", "份数"):
        v = raw(h)
        if v:
            try:
                int(float(v))
            except (TypeError, ValueError):
                errs.append((h, f"「{v}」需为数字，该列将保留原值"))
    for h in ("签订日期", "到期日期"):
        v = raw(h)
        if v and _parse_date_val(v) is None:
            errs.append((h, f"「{v}」格式无法识别（支持 2026-01-31 / 2026/1/31），该列将保留原值"))
    return errs


def _export_response(spec, include_header_only=False):
    """生成 xlsx 导出响应（table_export / table_template 共用）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = spec["label"][:30]
    headers = [c[0] for c in spec["columns"]]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="E6F7FF")
    if not include_header_only:
        for obj in spec["queryset"]():
            ws.append([c[1](obj) for c in spec["columns"]])
    buf = io.BytesIO()
    wb.save(buf)
    suffix = "模板" if include_header_only else "导出"
    fname = f"{spec['label']}{suffix}_{datetime.now():%Y%m%d_%H%M}.xlsx"
    resp = HttpResponse(
        buf.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = f"attachment; filename=export.xlsx; filename*=utf-8''{quote(fname)}"
    return resp


@login_required
@perm_required("archive", "read")
def table_export(request, table):
    """按表导出 xlsx（导出列 = 导入列，可直接修改后回导）。"""
    spec = _get_spec(table)
    _log(request, "导出", f"{spec['label']}导出", f"导出{spec['label']}清单")
    return _export_response(spec)


@login_required
@perm_required("archive", "read")
def table_template(request, table):
    """下载表级导入模板（仅表头 + 首条记录示例）。"""
    spec = _get_spec(table)
    return _export_response(spec, include_header_only=True)


@login_required
@perm_required("archive", "update")
def table_import(request, table):
    """表级两步导入：上传预览 → 确认执行（upsert：匹配键存在则仅更新非空列，不存在则新建）。"""
    spec = _get_spec(table)
    session_key = f"table_import_rows_{table}"
    columns = [c[0] for c in spec["columns"]]
    # 预览展示列：前 4 个可写入列（排除匹配键与只读列）
    preview_fields = [
        header for header, _g, setter in spec["columns"][:4]
        if header != spec["match_header"] and setter is not None
    ]

    if request.method == "GET":
        return render(request, "archive/table_import.html", {
            "spec": spec, "table": table, "columns": columns,
            "match_header": spec["match_header"],
        })

    if request.POST.get("step") == "upload":
        f = request.FILES.get("file")
        if not f:
            messages.error(request, "请选择 Excel 文件（.xlsx / .xls / .csv）")
            return redirect("archive:table_import", table=table)
        try:
            rows = _read_import_rows(f)
        except Exception as e:
            messages.error(request, f"文件解析失败：{e}")
            return redirect("archive:table_import", table=table)
        if not rows:
            messages.error(request, "未读取到有效数据（可能文件为空或表头不在第 1 行）")
            return redirect("archive:table_import", table=table)
        headers = list(rows[0].keys())
        if spec["match_header"] not in headers:
            messages.error(request, f"缺少必需的匹配键列「{spec['match_header']}」，请使用导出文件或下载模板填写")
            return redirect("archive:table_import", table=table)

        # 批量判定新增/更新（分批 IN 查询，避免超长参数列表）
        keys = [str(row.get(spec["match_header"], "") or "").strip() for row in rows]
        existing_keys = set()
        model = spec["model"]
        field = spec["match_field"]
        if field != "path":
            vals = [k for k in keys if k]
            for i in range(0, len(vals), 500):
                batch = vals[i:i + 500]
                if field == "id":
                    batch = [int(k) for k in batch if k.isdigit()]
                    if batch:
                        existing_keys.update(model.objects.filter(pk__in=batch).values_list("pk", flat=True))
                else:
                    existing_keys.update(model.objects.filter(**{f"{field}__in": batch}).values_list(field, flat=True))

        created_n = updated_n = 0
        preview = []
        bad_cells = []
        is_archive_spec = spec is TABLE_SPECS["archives"]
        for idx, key_raw in enumerate(keys):
            is_new = key_raw not in existing_keys
            if is_new:
                created_n += 1
            else:
                updated_n += 1
            row = rows[idx]
            if is_archive_spec:
                for col, reason in _validate_archive_row(row):
                    bad_cells.append({"line": idx + 2, "key": key_raw or "（空）", "col": col, "reason": reason})
            if len(preview) < 20:
                preview.append({
                    "key": key_raw or "（空）",
                    "action": "新增" if is_new else "更新",
                    "fields": [(h, str(row.get(h, "") or "")[:40]) for h in preview_fields],
                })
        request.session[session_key] = rows
        return render(request, "archive/table_import_confirm.html", {
            "spec": spec, "table": table, "preview": preview,
            "created_n": created_n, "updated_n": updated_n, "total": len(rows),
            "match_header": spec["match_header"], "preview_fields": preview_fields,
            "bad_cells": bad_cells,
        })

    if request.POST.get("step") == "commit":
        rows = request.session.get(session_key) or []
        if not rows:
            messages.error(request, "会话已过期，请重新上传文件")
            return redirect("archive:table_import", table=table)
        created_n = updated_n = 0
        errors = []
        skipped_cells = []
        is_archive_spec = spec is TABLE_SPECS["archives"]
        for i, row in enumerate(rows, start=2):
            key_raw = str(row.get(spec["match_header"], "") or "").strip()
            try:
                with transaction.atomic():
                    if spec["match_field"] == "path":
                        obj = _category_from_path(key_raw)
                        if obj is None:
                            raise ValueError("类别全路径为空")
                        created = obj._state.adding
                    else:
                        obj = _match_obj(spec, key_raw)
                        created = obj is None
                        if created:
                            if not key_raw:
                                raise ValueError(f"匹配键「{spec['match_header']}」为空，无法定位或新建")
                            if spec["match_field"] == "id":
                                raise ValueError(f"记录ID「{key_raw}」不存在，新建请走页面登记")
                            obj = spec["model"](**{spec["match_field"]: key_raw[:100]})
                    field_errors = [] if is_archive_spec else None
                    _apply_columns(spec, obj, row, is_created=created, field_errors=field_errors)
                    if hasattr(obj, "refresh_status"):
                        obj.refresh_status()
                    obj.save()
                    _finalize_cabinet(obj)
                    if created:
                        created_n += 1
                    else:
                        updated_n += 1
                    for col, reason in (field_errors or []):
                        skipped_cells.append(f"第{i}行[{key_raw or '空'}] {col}：{reason}")
            except Exception as e:
                errors.append(f"第{i}行[{key_raw or '空'}]：{e}")
        _log(request, "导入", f"{spec['label']}导入",
             f"新增 {created_n} 条，更新 {updated_n} 条，忽略单元格 {len(skipped_cells)}，失败 {len(errors)} 条")
        if created_n or updated_n:
            msg = f"{spec['label']}导入完成：新增 {created_n} 条，更新 {updated_n} 条"
            if skipped_cells:
                msg += f"；忽略 {len(skipped_cells)} 个无效单元格（保留原值）"
            messages.success(request, msg)
        for w in skipped_cells[:10]:
            messages.warning(request, w)
        if len(skipped_cells) > 10:
            messages.warning(request, f"……另有 {len(skipped_cells) - 10} 个忽略明细未展示")
        for err in errors[:10]:
            messages.warning(request, err)
        if len(errors) > 10:
            messages.warning(request, f"……另有 {len(errors) - 10} 条错误未展示")
        request.session.pop(session_key, None)
        return redirect(spec["redirect"])

    return redirect("archive:overview")


# ---------- 台账档案导入 / 导出（按台账维度：导出 → Excel 修改 → 导入更新 闭环） ----------

_ARCHIVE_COLS = TABLE_SPECS["archives"]["columns"]
_ARCHIVE_MATCH_HEADER = TABLE_SPECS["archives"]["match_header"]  # 「编号」


def _archive_book_bytes(sheets):
    """sheets: [(工作表名, 档案列表)] → xlsx 二进制。列结构与档案导出一致（一个台账一个工作表）。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    wb.remove(wb.active)
    for name, archives in sheets:
        ws = wb.create_sheet(title=(name or "档案")[:31])
        ws.append([c[0] for c in _ARCHIVE_COLS])
        for cell in ws[1]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="E6F7FF")
        for a in archives:
            ws.append([c[1](a) for c in _ARCHIVE_COLS])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _xlsx_response(data, fname):
    resp = HttpResponse(
        data,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = f"attachment; filename=export.xlsx; filename*=utf-8''{quote(fname)}"
    return resp


def _classify_archive_rows(rows):
    """按「编号」判定行的新增/更新。返回 (keys, created_n, updated_n, existing_keys)。"""
    keys = [str(r.get(_ARCHIVE_MATCH_HEADER, "") or "").strip() for r in rows]
    existing = set()
    vals = [k for k in keys if k]
    for i in range(0, len(vals), 500):
        batch = vals[i:i + 500]
        existing.update(Archive.objects.filter(code__in=batch).values_list("code", flat=True))
    created_n = sum(1 for k in keys if k not in existing)
    return keys, created_n, len(keys) - created_n, existing


def _upsert_archive_row(row, ledger):
    """单行 upsert：按「编号」匹配更新（仅非空列，无效单元格保留原值），不存在则新建并归入指定台账。

    返回 (是否新建, 忽略的无效单元格 [(列名, 原因)])。
    """
    key_raw = str(row.get(_ARCHIVE_MATCH_HEADER, "") or "").strip()
    field_errors = []
    with transaction.atomic():
        obj = Archive.objects.filter(code=key_raw).first() if key_raw else None
        created = obj is None
        if created:
            if not key_raw:
                raise ValueError("「编号」为空，无法定位或新建")
            obj = Archive(code=key_raw[:100], ledger=ledger)
        _apply_columns(TABLE_SPECS["archives"], obj, row, is_created=created, field_errors=field_errors)
        obj.ledger = ledger  # 归属以操作维度为准，不信任 Excel 中的台账列
        if hasattr(obj, "refresh_status"):
            obj.refresh_status()
        obj.save()
    return created, field_errors


@login_required
@perm_required("archive", "read")
def ledger_export(request, pk):
    """导出单个台账的全部档案（xlsx 单工作表，可修改后通过「导入更新」回导）。"""
    lg = get_object_or_404(Ledger, pk=pk)
    archives = lg.archives.select_related("cabinet", "category", "ledger").order_by("id")
    data = _archive_book_bytes([(lg.name, archives)])
    _log(request, "导出", f"台账#{lg.id}", f"导出台账「{lg.name}」全部档案 {archives.count()} 条")
    return _xlsx_response(data, f"{lg.name}_档案_{datetime.now():%Y%m%d_%H%M}.xlsx")


@login_required
@perm_required("archive", "read")
def ledger_batch_export(request):
    """批量导出勾选的台账：一个 xlsx，每个台账一个工作表（工作表名=台账名）。"""
    ids = request.POST.getlist("ledgers")
    if not ids and request.GET.get("ledgers"):
        ids = [x for x in request.GET.get("ledgers", "").split(",") if x.strip().isdigit()]
    ledgers = Ledger.objects.filter(pk__in=[i for i in ids if str(i).isdigit()]).order_by("id")
    if not ledgers:
        messages.error(request, "请先勾选要导出的台账")
        return redirect("archive:overview")
    sheets = [
        (lg.name, lg.archives.select_related("cabinet", "category", "ledger").order_by("id"))
        for lg in ledgers
    ]
    data = _archive_book_bytes(sheets)
    total = sum(len(s[1]) for s in sheets)
    _log(request, "导出", "台账批量导出", f"导出 {ledgers.count()} 个台账共 {total} 条档案")
    if ledgers.count() == 1:
        fname = f"{ledgers.first().name}_档案_{datetime.now():%Y%m%d_%H%M}.xlsx"
    else:
        fname = f"台账档案批量导出_{datetime.now():%Y%m%d_%H%M}.xlsx"
    return _xlsx_response(data, fname)


@login_required
@perm_required("archive", "update")
def ledger_import(request, pk):
    """单台账导入更新：上传 Excel（表头与本台账导出一致），按「编号」匹配更新，新增归入本台账。"""
    lg = get_object_or_404(Ledger, pk=pk)
    headers = [c[0] for c in _ARCHIVE_COLS]
    preview_fields = [
        h for h, _g, s in _ARCHIVE_COLS[:4]
        if h != _ARCHIVE_MATCH_HEADER and s is not None
    ]
    session_key = f"ledger_import_rows_{pk}"

    if request.method == "GET":
        return render(request, "archive/ledger_import.html", {
            "mode": "single", "ledger": lg, "title": f"导入更新「{lg.name}」",
            "columns": headers, "match_header": _ARCHIVE_MATCH_HEADER,
        })

    if request.POST.get("step") == "upload":
        f = request.FILES.get("file")
        if not f:
            messages.error(request, "请选择 Excel 文件（.xlsx / .xls / .csv）")
            return redirect("archive:ledger_import", pk=pk)
        try:
            rows = _read_import_rows(f)
        except Exception as e:
            messages.error(request, f"文件解析失败：{e}")
            return redirect("archive:ledger_import", pk=pk)
        if not rows:
            messages.error(request, "未读取到有效数据（可能文件为空或表头不在第 1 行）")
            return redirect("archive:ledger_import", pk=pk)
        if _ARCHIVE_MATCH_HEADER not in list(rows[0].keys()):
            messages.error(request, f"缺少必需的匹配键列「{_ARCHIVE_MATCH_HEADER}」，请使用本台账导出文件填写")
            return redirect("archive:ledger_import", pk=pk)
        # 归属强制为本台账（预览与提交一致）
        for r in rows:
            r["所属台账"] = lg.name
        keys, created_n, updated_n, existing = _classify_archive_rows(rows)
        # 行级预检：列出将忽略的无效单元格（导入时保留原值，不阻断导入）
        bad_cells = []
        for i, row in enumerate(rows, start=2):
            for col, reason in _validate_archive_row(row):
                bad_cells.append({"line": i, "key": str(row.get(_ARCHIVE_MATCH_HEADER, "") or "").strip(), "col": col, "reason": reason})
        preview = []
        for idx, k in enumerate(keys):
            if len(preview) < 20:
                row = rows[idx]
                preview.append({
                    "key": k or "（空）",
                    "action": "新增" if k not in existing else "更新",
                    "fields": [(h, str(row.get(h, "") or "")[:40]) for h in preview_fields],
                })
        request.session[session_key] = rows
        return render(request, "archive/table_import_confirm.html", {
            "title": f"确认导入「{lg.name}」", "reupload_url": request.path,
            "preview": preview, "created_n": created_n, "updated_n": updated_n,
            "total": len(rows), "match_header": _ARCHIVE_MATCH_HEADER,
            "preview_fields": preview_fields, "bad_cells": bad_cells,
        })

    if request.POST.get("step") == "commit":
        rows = request.session.get(session_key) or []
        if not rows:
            messages.error(request, "会话已过期，请重新上传文件")
            return redirect("archive:ledger_import", pk=pk)
        created_n = updated_n = 0
        errors = []
        skipped_cells = []
        for i, row in enumerate(rows, start=2):
            key_raw = str(row.get(_ARCHIVE_MATCH_HEADER, "") or "").strip()
            try:
                created, field_errors = _upsert_archive_row(row, lg)
                if created:
                    created_n += 1
                else:
                    updated_n += 1
                for col, reason in field_errors:
                    skipped_cells.append(f"第{i}行[{key_raw or '空'}] {col}：{reason}")
            except Exception as e:
                errors.append(f"第{i}行[{key_raw or '空'}]：{e}")
        _log(request, "导入", f"台账#{lg.id}",
             f"台账「{lg.name}」导入：新增 {created_n}，更新 {updated_n}，忽略单元格 {len(skipped_cells)}，失败 {len(errors)}")
        if created_n or updated_n:
            msg = f"台账「{lg.name}」导入完成：新增 {created_n} 条，更新 {updated_n} 条"
            if skipped_cells:
                msg += f"；忽略 {len(skipped_cells)} 个无效单元格（保留原值）"
            messages.success(request, msg)
        for w in skipped_cells[:10]:
            messages.warning(request, w)
        if len(skipped_cells) > 10:
            messages.warning(request, f"……另有 {len(skipped_cells) - 10} 个忽略明细未展示")
        for err in errors[:10]:
            messages.warning(request, err)
        if len(errors) > 10:
            messages.warning(request, f"……另有 {len(errors) - 10} 条错误未展示")
        request.session.pop(session_key, None)
        return redirect("archive:ledger_detail", pk=pk)

    return redirect("archive:overview")


@login_required
@perm_required("archive", "update")
def ledger_batch_import(request):
    """批量导入更新：上传多工作表 Excel，工作表名=台账名（不存在自动创建），按「编号」匹配更新。"""
    session_key = "ledger_batch_import_rows"
    headers = [c[0] for c in _ARCHIVE_COLS]
    preview_fields = [
        h for h, _g, s in _ARCHIVE_COLS[:4]
        if h != _ARCHIVE_MATCH_HEADER and s is not None
    ]

    if request.method == "GET":
        return render(request, "archive/ledger_import.html", {
            "mode": "batch", "title": "批量导入更新（按工作表匹配台账）",
            "columns": headers, "match_header": _ARCHIVE_MATCH_HEADER,
        })

    if request.POST.get("step") == "upload":
        f = request.FILES.get("file")
        if not f:
            messages.error(request, "请选择 Excel 文件（.xlsx / .xls）")
            return redirect("archive:ledger_batch_import")
        try:
            sheets = _parse_excel_sheets(f)
        except Exception as e:
            messages.error(request, f"文件解析失败：{e}")
            return redirect("archive:ledger_batch_import")
        if not sheets:
            messages.error(request, "未读取到有效数据（每个工作表需有表头行和数据行）")
            return redirect("archive:ledger_batch_import")
        bad = [name for name, data in sheets.items() if _ARCHIVE_MATCH_HEADER not in data["headers"]]
        if bad:
            messages.error(request, f"以下工作表缺少「{_ARCHIVE_MATCH_HEADER}」列：{'、'.join(bad)[:100]}")
            return redirect("archive:ledger_batch_import")

        existing_ledger_names = set(Ledger.objects.values_list("name", flat=True))
        all_rows = {}
        sheet_notes = []
        preview = []
        bad_cells = []
        grand_created = grand_updated = 0
        total = 0
        for name, data in sheets.items():
            rows = data["rows"]
            for r in rows:
                r["所属台账"] = name  # 归属以工作表名为准
            keys, created_n, updated_n, existing = _classify_archive_rows(rows)
            grand_created += created_n
            grand_updated += updated_n
            total += len(keys)
            all_rows[name] = rows
            sheet_notes.append({
                "name": name,
                "note": "新建台账" if name not in existing_ledger_names else "已有台账",
                "created": created_n, "updated": updated_n,
            })
            for i, row in enumerate(rows, start=2):
                for col, reason in _validate_archive_row(row):
                    bad_cells.append({"line": i, "key": f"{name} / {str(row.get(_ARCHIVE_MATCH_HEADER, '') or '').strip()}", "col": col, "reason": reason})
            for idx, k in enumerate(keys):
                if len(preview) >= 20:
                    break
                row = rows[idx]
                preview.append({
                    "key": f"{name} / {k or '（空）'}",
                    "action": "新增" if k not in existing else "更新",
                    "fields": [(h, str(row.get(h, "") or "")[:40]) for h in preview_fields],
                })
        request.session[session_key] = all_rows
        return render(request, "archive/table_import_confirm.html", {
            "title": "确认批量导入", "reupload_url": request.path,
            "preview": preview, "created_n": grand_created, "updated_n": grand_updated,
            "total": total, "match_header": _ARCHIVE_MATCH_HEADER,
            "preview_fields": preview_fields, "sheet_notes": sheet_notes, "bad_cells": bad_cells,
        })

    if request.POST.get("step") == "commit":
        all_rows = request.session.get(session_key) or {}
        if not all_rows:
            messages.error(request, "会话已过期，请重新上传文件")
            return redirect("archive:ledger_batch_import")
        created_n = updated_n = 0
        errors = []
        skipped_cells = []
        for name, rows in all_rows.items():
            lg, _ = Ledger.objects.get_or_create(name=name[:200])
            for i, row in enumerate(rows, start=2):
                key_raw = str(row.get(_ARCHIVE_MATCH_HEADER, "") or "").strip()
                try:
                    created, field_errors = _upsert_archive_row(row, lg)
                    if created:
                        created_n += 1
                    else:
                        updated_n += 1
                    for col, reason in field_errors:
                        skipped_cells.append(f"[{name}] 第{i}行[{key_raw or '空'}] {col}：{reason}")
                except Exception as e:
                    errors.append(f"[{name}] 第{i}行[{key_raw or '空'}]：{e}")
        _log(request, "导入", "台账批量导入",
             f"批量导入 {len(all_rows)} 个台账：新增 {created_n}，更新 {updated_n}，忽略单元格 {len(skipped_cells)}，失败 {len(errors)}")
        if created_n or updated_n:
            msg = f"批量导入完成：新增 {created_n} 条，更新 {updated_n} 条"
            if skipped_cells:
                msg += f"；忽略 {len(skipped_cells)} 个无效单元格（保留原值）"
            messages.success(request, msg)
        for w in skipped_cells[:10]:
            messages.warning(request, w)
        if len(skipped_cells) > 10:
            messages.warning(request, f"……另有 {len(skipped_cells) - 10} 个忽略明细未展示")
        for err in errors[:10]:
            messages.warning(request, err)
        if len(errors) > 10:
            messages.warning(request, f"……另有 {len(errors) - 10} 条错误未展示")
        request.session.pop(session_key, None)
        return redirect("archive:overview")

    return redirect("archive:overview")


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
    logs = OperationLog.objects.filter(module_type="档案管理").order_by("-operation_time")[:200]
    return render(request, "archive/log_list.html", {"logs": logs})


# ==================== 证照管理子模块 ====================


def _license_log(request, op_type: str, obj_id: str, summary: str):
    """写入证照操作留痕。"""
    OperationLog.objects.create(
        operator=_operator(request),
        module_type="证照管理",
        operation_type=op_type,
        object_identifier=obj_id,
        change_summary=summary,
    )


@login_required
@perm_required("archive", "read")
def license_list(request):
    """证照列表：仪表盘统计 + 搜索 + 状态筛选。"""
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")  # normal / expiring / expired

    qs = License.objects.all()
    if q:
        qs = qs.filter(
            Q(name__icontains=q)
            | Q(code__icontains=q)
            | Q(holder__icontains=q)
            | Q(issuer__icontains=q)
        )

    # 按 Python 属性筛选状态（因 status 是计算属性，非 DB 字段）
    all_licenses = list(qs)
    if status:
        all_licenses = [l for l in all_licenses if l.status == status]

    # 仪表盘统计
    total = License.objects.count()
    expiring_count = sum(1 for l in License.objects.all() if l.status == "expiring")
    expired_count = sum(1 for l in License.objects.all() if l.status == "expired")
    borrowed_count = sum(1 for l in License.objects.all() if l.is_borrowed)

    context = {
        "licenses": all_licenses,
        "q": q,
        "status": status,
        "total": total,
        "expiring_count": expiring_count,
        "expired_count": expired_count,
        "borrowed_count": borrowed_count,
    }
    return render(request, "archive/license_list.html", context)


@login_required
@perm_required("archive", "read")
def license_detail(request, pk):
    """证照详情：基本信息 + 年审历史 + 借阅记录 + 操作历史。"""
    lic = get_object_or_404(License, pk=pk)
    reviews = lic.reviews.all()[:20]
    borrows = lic.borrow_records.all()[:10]
    logs = (
        OperationLog.objects.filter(module_type="证照管理", object_identifier=f"证照#{lic.id}")
        .order_by("-operation_time")[:20]
    )
    context = {
        "license": lic,
        "reviews": reviews,
        "borrows": borrows,
        "logs": logs,
    }
    return render(request, "archive/license_detail.html", context)


@login_required
@perm_required("archive", "create")
def license_create(request):
    """新增证照（AJAX 弹窗）。"""
    form = LicenseForm(request.POST or None, request.FILES or None)
    if form.is_valid():
        lic = form.save(commit=False)
        lic.creator = request.user
        lic.save()
        _license_log(request, "新增", f"证照#{lic.id}", f"新增证照: {lic.code} - {lic.name}")
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": f"证照 {lic.code} 已录入"})
        return redirect("archive:license_detail", pk=lic.pk)
    if _is_ajax(request) and request.method == "POST":
        html = render(request, "archive/_license_form_modal.html", {"form": form, "mode": "create"}).content.decode()
        return JsonResponse({"ok": False, "html": html}, status=422)
    if _is_ajax(request):
        html = render(request, "archive/_license_form_modal.html", {"form": form, "mode": "create"}).content.decode()
        return JsonResponse({"ok": True, "html": html})
    return render(request, "archive/license_form.html", {"form": form, "mode": "create"})


@login_required
@perm_required("archive", "update")
def license_edit(request, pk):
    """编辑证照（AJAX 弹窗）。"""
    lic = get_object_or_404(License, pk=pk)
    form = LicenseForm(request.POST or None, request.FILES or None, instance=lic)
    if form.is_valid():
        lic = form.save()
        _license_log(request, "编辑", f"证照#{lic.id}", f"编辑证照: {lic.code} - {lic.name}")
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": "证照已更新"})
        return redirect("archive:license_detail", pk=lic.pk)
    if _is_ajax(request) and request.method == "POST":
        html = render(request, "archive/_license_form_modal.html", {"form": form, "mode": "edit", "license": lic}).content.decode()
        return JsonResponse({"ok": False, "html": html}, status=422)
    if _is_ajax(request):
        html = render(request, "archive/_license_form_modal.html", {"form": form, "mode": "edit", "license": lic}).content.decode()
        return JsonResponse({"ok": True, "html": html})
    return render(request, "archive/license_form.html", {"form": form, "mode": "edit", "license": lic})


@login_required
@perm_required("archive", "delete")
def license_delete(request, pk):
    """删除证照（硬删除）。"""
    lic = get_object_or_404(License, pk=pk)
    if request.method == "POST":
        _license_log(request, "删除", f"证照#{lic.id}", f"删除证照: {lic.code} - {lic.name}")
        # 删除关联的物理扫描件
        if lic.scan_file:
            path = lic.scan_file.path
            if path and os.path.exists(path):
                os.remove(path)
        lic.delete()
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": "证照已删除"})
        return redirect("archive:license_list")
    return render(request, "archive/license_confirm_delete.html", {"license": lic})


@login_required
@perm_required("archive", "create")
def annual_review_create(request, license_pk):
    """年审登记（AJAX 弹窗）。年审通过后自动更新证照有效期和下次年审日。"""
    lic = get_object_or_404(License, pk=license_pk)
    form = AnnualReviewForm(request.POST or None)
    if form.is_valid():
        review = form.save(commit=False)
        review.license = lic
        review.save()
        # 年审通过后自动续期：更新证照有效期和下次年审日
        if review.result == "passed":
            if review.new_expire_date:
                lic.expire_date = review.new_expire_date
            if lic.review_cycle:
                lic.next_review_date = _add_months(review.review_date, lic.review_cycle)
            lic.save(update_fields=["expire_date", "next_review_date", "updated_at"])
        _license_log(request, "年审", f"证照#{lic.id}", f"年审登记: {lic.code} - {review.get_result_display()}")
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": "年审登记成功"})
        return redirect("archive:license_detail", pk=lic.pk)
    if _is_ajax(request):
        html = render_to_string(
            "archive/_license_review_modal.html", {"form": form, "license": lic}, request=request
        )
        if request.method == "POST":
            return JsonResponse({"ok": False, "html": html}, status=422)
        return JsonResponse({"ok": True, "html": html})
    return render(request, "archive/license_review_form.html", {"form": form, "license": lic})


@login_required
@perm_required("archive", "read")
def license_borrow_list(request):
    """证照借阅记录列表。"""
    status = request.GET.get("status")
    qs = LicenseBorrowRecord.objects.select_related("license")
    if status:
        for r in qs.filter(return_time__isnull=True):
            r.refresh_status()
            r.save(update_fields=["status"])
        qs = qs.filter(status=status)
    return render(request, "archive/license_borrow_list.html", {"records": qs, "status": status})


@login_required
@perm_required("archive", "create")
def license_borrow_create(request, license_pk):
    """证照借阅登记（AJAX 弹窗）。"""
    lic = get_object_or_404(License, pk=license_pk)
    if lic.is_borrowed:
        if _is_ajax(request):
            return JsonResponse({"ok": False, "message": f"证照 {lic.code} 已借出，无法再次借阅"}, status=422)
        messages.error(request, f"证照 {lic.code} 已借出")
        return redirect("archive:license_detail", pk=lic.pk)
    form = LicenseBorrowForm(request.POST or None)
    if form.is_valid():
        rec = form.save(commit=False)
        rec.license = lic
        rec.save()
        _license_log(request, "借阅", f"证照#{lic.id}", f"借出证照: {lic.code} 给 {rec.borrower}，应还 {rec.due_time}")
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": "借阅登记成功"})
        return redirect("archive:license_detail", pk=lic.pk)
    if _is_ajax(request):
        html = render_to_string(
            "archive/_license_borrow_modal.html", {"form": form, "license": lic}, request=request
        )
        if request.method == "POST":
            return JsonResponse({"ok": False, "html": html}, status=422)
        return JsonResponse({"ok": True, "html": html})
    return render(request, "archive/license_borrow_form.html", {"form": form, "license": lic})


@login_required
@perm_required("archive", "update")
def license_borrow_return(request, pk):
    """证照归还。"""
    rec = get_object_or_404(LicenseBorrowRecord, pk=pk)
    if request.method == "POST":
        rec.return_time = timezone.now()
        rec.refresh_status()
        rec.save()
        _license_log(request, "归还", f"证照#{rec.license_id}", f"归还证照: {rec.license.code}")
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": "归还成功"})
        return redirect("archive:license_borrow_list")
    return render(request, "archive/license_borrow_confirm_return.html", {"record": rec})


# ---------- 证照批量导入 ----------

LICENSE_FIELD_CHOICES = [
    ("name", "证照名称"),
    ("code", "证照编号"),
    ("holder", "持证单位/保管部门"),
    ("issuer", "发证机关/授予机关"),
    ("issue_date", "发证/颁发日期"),
    ("expire_date", "有效期至"),
    ("next_review_date", "下次年审/到期预警日"),
    ("review_cycle", "年审周期(月,文本自动解析)"),
    ("last_review", "最近年审时间(自动建年审记录)"),
    ("remark", "备注/变更记录"),
]

LICENSE_KEYWORD_MAP = {
    "name": ["证照名称", "荣誉名称", "名称", "标题"],
    "code": ["证照编号", "编号", "证书编号"],
    "holder": ["持证单位", "保管部门", "保管部门/人", "荣誉归属", "归属"],
    "issuer": ["发证机关", "授予机关", "颁发机关", "发证单位"],
    "issue_date": ["发证日期", "颁发日期", "签发日期"],
    "expire_date": ["有效期截至", "有效期至", "到期日期", "截止日期"],
    "next_review_date": ["到期预警日", "下次年审", "年审日期"],
    "review_cycle": ["年审/年检要求", "年审要求", "年检要求", "年审", "年检"],
    "last_review": ["最近年审时间", "最近年审", "最近年审日期"],
    "remark": ["备注", "变更记录", "变更", "说明"],
}


def _auto_map_license_columns(headers):
    """根据表头文本自动推断证照字段映射（子串匹配，允许备注多列映射）。"""
    mapping = {}
    for col in headers:
        col_s = col.strip()
        for field, keywords in LICENSE_KEYWORD_MAP.items():
            if any(kw in col_s for kw in keywords):
                # remark 允许多列映射（变更记录 + 备注都导入）
                if field == "remark" or field not in mapping.values():
                    mapping[col] = field
                    break
    return mapping


def _parse_review_cycle(text):
    """解析年审周期文本为月数。'一年一审' → 12, '三年换证' → 36。"""
    if not text or text.strip() in ("/", "-", "无", "不需要", "否"):
        return 0
    match = re.search(r"(\d+)\s*年", text)
    if match:
        return int(match.group(1)) * 12
    match = re.search(r"(\d+)\s*个?月", text)
    if match:
        return int(match.group(1))
    return 0


def _parse_date_loose(raw_val):
    """宽松日期解析，支持 datetime 字符串和多种格式。返回 date 或 None。"""
    if not raw_val:
        return None
    raw_val = raw_val.strip()
    if " " in raw_val:
        raw_val = raw_val.split(" ")[0]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw_val, fmt).date()
        except ValueError:
            continue
    # 尝试补全不完整日期如 "2026/1/" → "2026/1/1"
    parts = re.sub(r"[/.]", "/", raw_val).split("/")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        try:
            y, m = int(parts[0]), int(parts[1])
            d = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 1
            return date(y, m, d)
        except (ValueError, IndexError):
            pass
    return None


@login_required
@perm_required("archive", "create")
def import_licenses(request):
    """证照批量导入：上传 → 列映射 → 执行导入。"""
    if request.method == "GET":
        return render(request, "archive/license_import_step1.html", {})

    # ---- Step 1: 上传文件 → 解析 sheet → 返回列映射页面 ----
    if request.POST.get("step") == "upload":
        f = request.FILES.get("file")
        if not f:
            messages.error(request, "请选择 Excel 文件（.xlsx / .xls）")
            return render(request, "archive/license_import_step1.html", {})
        try:
            sheets = _parse_excel_sheets(f)
        except Exception as e:
            messages.error(request, f"文件解析失败：{e}")
            return render(request, "archive/license_import_step1.html", {})
        if not sheets:
            messages.error(request, "未读取到有效数据（可能 sheet 为空或表头不在第 1 行）")
            return render(request, "archive/license_import_step1.html", {})
        for sname, sdata in sheets.items():
            auto_map = _auto_map_license_columns(sdata["headers"])
            sdata["auto_mapping"] = auto_map
            first_row = sdata["rows"][0] if sdata["rows"] else {}
            sdata["header_choices"] = [
                {
                    "col": col,
                    "auto": auto_map.get(col, ""),
                    "sample": first_row.get(col, ""),
                }
                for col in sdata["headers"]
            ]
        request.session["license_import_sheets"] = sheets
        return render(request, "archive/license_import_step2.html", {
            "sheets": sheets, "field_choices": LICENSE_FIELD_CHOICES,
        })

    # ---- Step 2: 执行导入 ----
    if request.POST.get("step") == "import":
        sheets = request.session.get("license_import_sheets", {})
        if not sheets:
            messages.error(request, "会话已过期，请重新上传文件")
            return render(request, "archive/license_import_step1.html", {})

        total_created = 0
        total_reviews = 0
        total_errors = []

        for sname, sdata in sheets.items():
            mapping = {}
            for col in sdata["headers"]:
                val = request.POST.get(f"map__{sname}__{col}", "")
                if val:
                    mapping[col] = val

            has_name = any(v == "name" for v in mapping.values())
            if not has_name:
                total_errors.append(f"[{sname}] 未映射「证照名称」列，跳过")
                continue

            created_codes = set()  # 跟踪本批次已创建的编号

            for i, row in enumerate(sdata["rows"], start=2):
                try:
                    obj = {"creator": request.user}
                    last_review_date = None
                    remark_parts = []

                    for col, val in row.items():
                        field = mapping.get(col)
                        if not field:
                            continue
                        raw_val = val.strip()
                        if not raw_val:
                            continue

                        if field == "last_review":
                            last_review_date = _parse_date_loose(raw_val)
                        elif field in ("issue_date", "expire_date", "next_review_date"):
                            parsed = _parse_date_loose(raw_val)
                            if parsed:
                                obj[field] = parsed
                        elif field == "review_cycle":
                            obj["review_cycle"] = _parse_review_cycle(raw_val)
                        elif field == "remark":
                            remark_parts.append(raw_val)
                        else:
                            obj[field] = raw_val[:200]

                    if not obj.get("name"):
                        continue

                    # 处理编号："/" 等占位符视为空，自动生成
                    code_val = obj.get("code", "")
                    if not code_val or code_val in ("/", "-", "无", "—"):
                        obj.pop("code", None)
                        import random
                        import string
                        code_val = "LIC" + "".join(random.choices(string.digits, k=8))

                    # 确保 DB 内和本批次内唯一
                    while code_val in created_codes or License.objects.filter(code=code_val).exists():
                        code_val = code_val + "D"
                    obj["code"] = code_val
                    created_codes.add(code_val)

                    if remark_parts:
                        obj["remark"] = " | ".join(remark_parts)

                    lic = License.objects.create(**obj)
                    total_created += 1

                    # 如有最近年审时间，自动创建年审记录
                    if last_review_date:
                        AnnualReview.objects.create(
                            license=lic,
                            review_date=last_review_date,
                            result="passed",
                            reviewer=_operator(request),
                            remark="台账导入自动生成",
                        )
                        total_reviews += 1

                except Exception as e:
                    total_errors.append(f"[{sname}]第{i}行：{e}")

        _license_log(request, "导入", "证照批量导入",
                     f"成功 {total_created} 条证照，{total_reviews} 条年审记录，失败 {len(total_errors)} 条")

        if total_created:
            messages.success(
                request,
                f"成功导入 {total_created} 条证照"
                + (f"，{total_reviews} 条年审记录" if total_reviews else ""),
            )
        for err in total_errors[:10]:
            messages.warning(request, err)
        if len(total_errors) > 10:
            messages.warning(request, f"……另有 {len(total_errors) - 10} 条错误未展示")

        del request.session["license_import_sheets"]
        return redirect("archive:license_list")

    return redirect("archive:license_list")
