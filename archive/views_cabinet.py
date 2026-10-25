"""档案柜域视图（列表/CRUD/层配置）。

从 views.py 拆出的独立业务域。URL 配置仍通过 views.py 门面 re-export 引用，
URL 名称与行为完全不变。
"""

import json
from collections import defaultdict

from django.contrib import messages

from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from accounts.decorators import perm_required
from records.crud import _per_page

from .forms import CabinetForm
from .models import Archive, Cabinet, CabinetLayer
from .services.common import _is_ajax, _log


@perm_required("archive", "read")
def cabinet_list(request):
    """档案柜列表：编号、位置、各层格数与空位占用一览。

    三批聚合/枚举查询预取数据（层配置、占用格、档案数），
    替代原逐柜渲染时触发模型属性查询的 N+1；数值口径与模型属性完全一致。
    """
    cells_map = defaultdict(list)
    for row in CabinetLayer.objects.values("cabinet_id", "layer_no", "cells").order_by("cabinet_id", "layer_no"):
        cells_map[row["cabinet_id"]].append(row["cells"])
    used_map = defaultdict(set)
    for cab_id, layer, cell in Archive.objects.values_list("cabinet_id", "layer", "cell"):
        used_map[cab_id].add((layer, cell))
    count_map = dict(
        Archive.objects.values("cabinet_id").annotate(n=Count("id")).values_list("cabinet_id", "n")
    )
    rows = []
    for cab in Cabinet.objects.all():
        cells = cells_map.get(cab.pk, [])
        total = sum(cells)
        used = len(used_map.get(cab.pk, ()))
        rows.append({
            "cab": cab,
            "layer_cells_summary": ",".join(map(str, cells)) if cells else "—",
            "total_cells": total,
            "used_cells": used,
            "free_cells": total - used,
            "archive_count": count_map.get(cab.pk, 0),
        })
    # 后端分页（10/30/50）：rows 为聚合后的内存列表，Paginator 同样适用
    per_page = _per_page(request)
    page_obj = Paginator(rows, per_page).get_page(request.GET.get("page"))
    return render(request, "archive/cabinet_list.html", {
        "rows": page_obj, "page_obj": page_obj, "per_page": per_page,
    })


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
