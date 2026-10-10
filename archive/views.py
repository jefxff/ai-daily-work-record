"""档案管理模块视图。

功能：档案柜/档案/类别 CRUD、档案总览网格、详情、借阅、导入导出、二维码。
权限：基于 RBAC，模块 key=archive。验收前仅超管可见（registry 不授予内置角色）。
"""

import csv
import io
import os
from datetime import date, datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone

from accounts.decorators import perm_required
from records.models import OperationLog

from .forms import ArchiveForm, BorrowForm, CabinetForm, CategoryForm, LicenseForm, AnnualReviewForm, LicenseBorrowForm
from .models import Archive, BorrowRecord, Cabinet, Category, License, LicenseBorrowRecord, _add_months


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
    """档案总览页：顶部仪表盘 + 各档案柜按层-格网格展示。

    搜索维度（aq）：档案名称/编号/相对方/合同号/部门/位置编码/标签/经办人/描述。
    到期筛选（status=expiring）：仅显示 30 天内到期的档案格。
    """
    q = request.GET.get("q", "").strip()
    category_id = request.GET.get("category")
    status = request.GET.get("status")  # all / occupied / free / expiring
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

    # 即将到期档案数（30 天内到期，含今日）
    today = date.today()
    expire_threshold = today + timedelta(days=30)
    expiring_count = Archive.objects.filter(
        expire_date__isnull=False, expire_date__lte=expire_threshold, expire_date__gte=today
    ).count()

    # 档案搜索：按多字段匹配，网格中高亮命中的档案格
    aq = request.GET.get("aq", "").strip()
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

    # 即将到期档案 id 集合（status=expiring 时用于网格高亮/筛选）
    expiring_ids = []
    if status == "expiring":
        expiring_ids = list(
            Archive.objects.filter(
                expire_date__isnull=False, expire_date__lte=expire_threshold, expire_date__gte=today
            ).values_list("id", flat=True)
        )

    # 为每个柜构建网格（层 x 格），状态筛选在模板层处理
    cabinet_data = []
    for cab in cabinets:
        matrix = cab.cell_matrix()
        if status == "occupied":
            # 只展示有档案的行
            matrix = [[a for a in row if a] for row in matrix]
        elif status == "expiring":
            # 只展示即将到期档案所在的格子
            matrix = [[a for a in row if a and a.id in expiring_ids] for row in matrix]
            # 过滤掉空行
            matrix = [row for row in matrix if row]
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
        "expiring_ids": expiring_ids,
        "total_cabinets": total_cabinets,
        "total_archives": total_archives,
        "total_cells": total_cells,
        "used_cells": used_cells,
        "free_cells": total_cells - used_cells,
        "occupancy": occupancy,
        "overdue_count": overdue_count,
        "expiring_count": expiring_count,
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
        return render(request, "archive/import_step2.html", {
            "sheets": sheets, "field_choices": FIELD_CHOICES,
        })

    # ---- Step 2: 执行导入 ----
    if request.POST.get("step") == "import":
        sheets = request.session.get("import_sheets", {})
        if not sheets:
            messages.error(request, "会话已过期，请重新上传文件")
            return render(request, "archive/import_step1.html", {})
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
        return redirect("archive:overview")

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
