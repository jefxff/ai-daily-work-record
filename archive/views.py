"""档案管理模块视图。

功能：档案柜/档案/类别 CRUD、档案总览网格、详情、借阅、导入导出、二维码。
权限：基于 RBAC，模块 key=archive。验收前仅超管可见（registry 不授予内置角色）。
"""

import csv
import io
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from accounts.decorators import perm_required
from records.models import OperationLog

from .forms import ArchiveForm, BorrowForm, CabinetForm, CategoryForm
from .models import Archive, BorrowRecord, Cabinet, Category


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


# ---------- 档案总览 ----------

@login_required
@perm_required("archive", "read")
def overview(request):
    """档案总览页：顶部仪表盘 + 各档案柜按层-格网格展示。"""
    q = request.GET.get("q", "").strip()
    category_id = request.GET.get("category")
    status = request.GET.get("status")  # all / occupied / free
    sort = request.GET.get("sort", "code")

    cabinets = Cabinet.objects.all()
    if q:
        cabinets = cabinets.filter(Q(code__icontains=q) | Q(location__icontains=q))
    if category_id:
        cabinets = cabinets.filter(archives__category_id=category_id).distinct()

    order_map = {"code": "code", "-code": "-code", "location": "location", "used": None}
    if sort == "used":
        cabinets = sorted(cabinets, key=lambda c: c.used_cells, reverse=True)
    elif sort in order_map and order_map[sort]:
        cabinets = cabinets.order_by(order_map[sort])

    # 全局仪表盘指标
    total_cabinets = Cabinet.objects.count()
    total_archives = Archive.objects.count()
    total_cells = sum(c.total_cells for c in Cabinet.objects.all())
    used_cells = Archive.objects.values("cabinet_id", "layer", "cell").distinct().count()
    occupancy = round(used_cells * 100 / total_cells, 1) if total_cells else 0.0
    overdue_count = BorrowRecord.objects.filter(return_time__isnull=True, due_time__lt=datetime.now().date()).count()

    # 档案搜索：按名称/编号/标签/描述匹配，网格中高亮命中的档案格
    aq = request.GET.get("aq", "").strip()
    matched_ids = []
    if aq:
        matched_ids = list(
            Archive.objects.filter(
                Q(name__icontains=aq)
                | Q(code__icontains=aq)
                | Q(tags__icontains=aq)
                | Q(description__icontains=aq)
            ).values_list("id", flat=True)
        )

    # 为每个柜构建网格（层 x 格），状态筛选在模板层处理
    cabinet_data = []
    for cab in cabinets:
        matrix = cab.cell_matrix()
        if status == "occupied":
            # 只展示有档案的行
            matrix = [[a for a in row if a] for row in matrix]
        cabinet_data.append({"cabinet": cab, "matrix": matrix})

    categories = Category.objects.all()

    context = {
        "cabinet_data": cabinet_data,
        "categories": categories,
        "q": q,
        "category_id": category_id,
        "status": status,
        "sort": sort,
        "aq": aq,
        "matched_ids": matched_ids,
        "match_count": len(matched_ids),
        "total_cabinets": total_cabinets,
        "total_archives": total_archives,
        "total_cells": total_cells,
        "used_cells": used_cells,
        "free_cells": total_cells - used_cells,
        "occupancy": occupancy,
        "overdue_count": overdue_count,
    }
    return render(request, "archive/overview.html", context)


# ---------- 档案柜 CRUD ----------

@login_required
@perm_required("archive", "create")
def cabinet_create(request):
    form = CabinetForm(request.POST or None)
    if form.is_valid():
        cab = form.save()
        _log(request, "新增", f"档案柜#{cab.id}", f"新增档案柜: {cab.code}（{cab.location}）")
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": f"档案柜 {cab.code} 已创建"})
        return redirect("archive:overview")
    if _is_ajax(request) and request.method == "POST":
        html = render(request, "archive/_cabinet_form_modal.html", {"form": form, "mode": "create"}).content.decode()
        return JsonResponse({"ok": False, "html": html}, status=422)
    if _is_ajax(request):
        html = render(request, "archive/_cabinet_form_modal.html", {"form": form, "mode": "create"}).content.decode()
        return JsonResponse({"ok": True, "html": html})
    return render(request, "archive/cabinet_form.html", {"form": form, "mode": "create"})


@login_required
@perm_required("archive", "update")
def cabinet_edit(request, pk):
    cab = get_object_or_404(Cabinet, pk=pk)
    form = CabinetForm(request.POST or None, instance=cab)
    if form.is_valid():
        cab = form.save()
        _log(request, "编辑", f"档案柜#{cab.id}", f"编辑档案柜: {cab.code}")
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": "档案柜已更新"})
        return redirect("archive:overview")
    if _is_ajax(request) and request.method == "POST":
        html = render(request, "archive/_cabinet_form_modal.html", {"form": form, "mode": "edit", "cabinet": cab}).content.decode()
        return JsonResponse({"ok": False, "html": html}, status=422)
    if _is_ajax(request):
        html = render(request, "archive/_cabinet_form_modal.html", {"form": form, "mode": "edit", "cabinet": cab}).content.decode()
        return JsonResponse({"ok": True, "html": html})
    return render(request, "archive/cabinet_form.html", {"form": form, "mode": "edit", "cabinet": cab})


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
        "borrows": borrows,
        "logs": logs,
        "free_cells": arc.cabinet.free_cells,
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
    qs = BorrowRecord.objects.select_related("archive", "borrower")
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
        return redirect("archive:archive_detail", pk=arc.pk)
    form = BorrowForm(request.POST or None)
    if form.is_valid():
        rec = form.save(commit=False)
        rec.archive = arc
        rec.save()
        _log(request, "借阅", f"档案#{arc.id}", f"借出档案: {arc.code} 给 {rec.borrower.username}，应还 {rec.due_time}")
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": "借阅登记成功"})
        return redirect("archive:archive_detail", pk=arc.pk)
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
    """解析上传的导入文件，返回字典行列表。支持 .xlsx 与 .csv。"""
    name = (getattr(f, "name", "") or "").lower()
    if name.endswith(".xlsx"):
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
                continue  # 跳过空行
            out.append({h: ("" if v is None else str(v).strip()) for h, v in zip(headers, r)})
        return out
    # CSV（兼容 utf-8 / utf-8-sig BOM）
    raw = f.read()
    content = None
    for enc in ("utf-8-sig", "gbk"):
        try:
            content = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if content is None:
        raise ValueError("文件编码无法识别，请使用模板中的 UTF-8 编码 CSV")
    return list(csv.DictReader(io.StringIO(content)))


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
    if request.method != "POST":
        return redirect("archive:overview")
    f = request.FILES.get("file")
    if not f:
        messages.error(request, "请选择 .xlsx 或 .csv 文件")
        return redirect("archive:overview")
    try:
        rows = _read_import_rows(f)
        created = 0
        errors = []
        for i, row in enumerate(rows, start=2):
            try:
                code = (row.get("编号") or "").strip()
                name = (row.get("名称") or "").strip()
                cab_code = (row.get("档案柜") or "").strip()
                tags = (row.get("标签") or "").strip()
                cat_name = (row.get("类别") or "").strip()
                description = (row.get("描述") or "").strip()
                if not code or not name or not cab_code:
                    errors.append(f"第{i}行：编号/名称/档案柜不能为空")
                    continue
                cab = Cabinet.objects.filter(code=cab_code).first()
                if not cab:
                    errors.append(f"第{i}行：档案柜 {cab_code} 不存在")
                    continue
                try:
                    layer = int(float(row.get("层") or 0))
                    cell_no = int(float(row.get("格") or 0))
                except ValueError:
                    errors.append(f"第{i}行：层/格必须是数字")
                    continue
                if layer < 1 or layer > cab.layers or cell_no < 1 or cell_no > cab.cells_per_layer:
                    errors.append(f"第{i}行：层/格超出 {cab.code} 容量（{cab.layers}层×{cab.cells_per_layer}格）")
                    continue
                if Archive.objects.filter(cabinet=cab, layer=layer, cell=cell_no).exists():
                    errors.append(f"第{i}行：位置 {cab_code}-{layer}层-{cell_no}格 已被占用")
                    continue
                if Archive.objects.filter(code=code).exists():
                    errors.append(f"第{i}行：编号 {code} 已存在")
                    continue
                category = Category.objects.filter(name=cat_name).first() if cat_name else None
                Archive.objects.create(
                    code=code, name=name, cabinet=cab, layer=layer, cell=cell_no,
                    tags=tags, category=category, description=description,
                    creator=request.user,
                )
                created += 1
            except Exception as e:
                errors.append(f"第{i}行：{e}")
        _log(request, "导入", "档案批量导入", f"成功导入 {created} 条，失败 {len(errors)} 条")
        if created:
            messages.success(request, f"成功导入 {created} 条档案")
        for err in errors[:10]:
            messages.warning(request, err)
        if len(errors) > 10:
            messages.warning(request, f"……另有 {len(errors) - 10} 条错误未展示")
        if not created and not errors:
            messages.warning(request, "未读取到有效数据行，请按模板格式填写")
    except Exception as e:
        messages.error(request, f"导入失败：{e}")
    return redirect("archive:overview")


@login_required
@perm_required("archive", "read")
def export_archives(request):
    response = HttpResponse(content_type="text/csv; charset=utf-8-sig")
    response["Content-Disposition"] = f'attachment; filename="archives_{datetime.now():%Y%m%d_%H%M}.csv"'
    writer = csv.writer(response)
    writer.writerow(["编号", "名称", "档案柜", "位置", "层", "格", "类别", "标签", "创建人", "创建时间"])
    for a in Archive.objects.select_related("cabinet", "category", "creator"):
        writer.writerow([
            a.code, a.name, a.cabinet.code, a.cabinet.location, a.layer, a.cell,
            a.category.name if a.category else "", a.tags,
            a.creator.username if a.creator else "",
            a.created_at.strftime("%Y-%m-%d %H:%M"),
        ])
    _log(request, "导出", "档案清单导出", "导出全部档案清单")
    return response


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
