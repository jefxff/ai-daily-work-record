"""导入 / 导出域视图（模板下载、智能导入、CSV 导出、表级通用导入导出）。

业务引擎本体位于 services/import_export.py，本模块仅保留请求/响应处理薄壳。
URL 配置仍通过 views.py 门面 re-export 引用，URL 名称与行为完全不变。
"""

import csv
import io
import logging
import os
from django.utils import timezone

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from accounts.decorators import perm_required

from .models import Archive, Ledger
from .services.common import _log
from .services.import_export import (
    FIELD_CHOICES, TABLE_SPECS, _apply_columns, _auto_map_columns,
    _category_from_path, _csv_safe, _export_response, _finalize_cabinet,
    _get_spec, _match_obj, _parse_excel_sheets, _read_import_rows,
    _validate_archive_row,
)

logger = logging.getLogger(__name__)


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
                                    logger.exception(
                                        "导入台账：解析日期字段失败，跳过该值 field=%s value=%s",
                                        field, raw_val)
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
                except IntegrityError:
                    # 同一柜/层/格重复写入：DB 唯一约束拦下（评审 R1）
                    total_errors.append(f"[{sname}]第{i}行：该存放位置（柜/层/格）已被其他档案占用，已跳过")
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
    response["Content-Disposition"] = f'attachment; filename="archives_{timezone.localtime():%Y%m%d_%H%M}.csv"'
    writer = csv.writer(response)
    writer.writerow(["所属台账", "来源Sheet", "编号", "名称", "档案柜", "位置", "层", "格", "类别", "标签", "创建人", "创建时间"])
    for a in Archive.objects.select_related("cabinet", "category", "creator", "ledger"):
        writer.writerow([
            _csv_safe(a.ledger.name if a.ledger else ""), _csv_safe(a.source_sheet),
            _csv_safe(a.code), _csv_safe(a.name),
            _csv_safe(a.cabinet.code if a.cabinet_id else ""), _csv_safe(a.cabinet.location if a.cabinet_id else ""),
            a.layer if a.layer is not None else "", a.cell if a.cell is not None else "",
            _csv_safe(a.category.name if a.category else ""), _csv_safe(a.tags),
            _csv_safe(a.creator.username if a.creator else ""),
            a.created_at.strftime("%Y-%m-%d %H:%M"),
        ])
    _log(request, "导出", "档案清单导出", "导出全部档案清单")
    return response


@login_required
@perm_required("archive", "read")
def table_export(request, table):
    """按表导出 xlsx（导出列 = 导入列，可直接修改后回导）。"""
    spec = _get_spec(table)
    # 审查 H5/B3：与其它导出共用并发闸门，避免多人同时导出打满 runserver 线程
    from work_record.export_guard import ExportSlot, export_busy_response

    slot = ExportSlot()
    if not slot.__enter__():
        return export_busy_response()
    try:
        _log(request, "导出", f"{spec['label']}导出", f"导出{spec['label']}清单")
        return _export_response(spec)
    finally:
        slot.__exit__(None, None, None)


@login_required
@perm_required("archive", "read")
def table_template(request, table):
    """下载表级导入模板（仅表头 + 首条记录示例）。"""
    spec = _get_spec(table)
    _log(request, "下载", f"{spec['label']}模板", f"下载{spec['label']}导入模板")
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
            except IntegrityError:
                # 柜/层/格重复：唯一约束兜底（评审 R1），逐行回滚不影响其他行
                errors.append(f"第{i}行[{key_raw or '空'}]：该存放位置（柜/层/格）已被其他档案占用，已跳过")
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
