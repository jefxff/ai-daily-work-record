"""档案总览与台账域视图（详情/CRUD/排序/删除/台账维度导入导出）。

从 views.py 拆出的独立业务域。URL 配置仍通过 views.py 门面 re-export 引用，
URL 名称与行为完全不变。
"""

from datetime import date, datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from accounts.decorators import perm_required
from records.crud import _per_page

from .forms import LedgerForm
from .models import Archive, BorrowRecord, Category, Ledger
from .services.common import _is_ajax, _log
from .services.import_export import (
    _ARCHIVE_COLS, _ARCHIVE_MATCH_HEADER, _archive_book_bytes,
    _classify_archive_rows, _parse_excel_sheets, _read_import_rows,
    _upsert_archive_row, _validate_archive_row, _xlsx_response,
)


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
        with transaction.atomic():
            lg.archives.all().delete()  # 级联删除台账下全部档案（附件、借出记录随之删除）
            lg.delete()
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": f"台账已删除（含档案 {cnt} 份）"})
        messages.success(request, f"台账 {name} 已删除（含档案 {cnt} 份）")
        return redirect("archive:overview")
    return render(request, "archive/ledger_confirm_delete.html", {
        "ledger": lg, "archive_count": lg.archives.count(),
    })


# ---------- 台账档案导入 / 导出（按台账维度：导出 → Excel 修改 → 导入更新 闭环） ----------

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
