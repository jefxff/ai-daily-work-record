"""证照管理子模块视图。

从 views.py 拆出的独立业务域：证照 CRUD、年审、借还、批量导入。
URL 配置仍通过 views.py 门面 re-export 引用，URL 名称与行为完全不变。
"""

import os
import re
from datetime import date, datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone

from accounts.decorators import perm_required
from records.crud import _per_page
from records.models import OperationLog

from .forms import AnnualReviewForm, LicenseBorrowForm, LicenseForm
from .models import AnnualReview, License, LicenseBorrowRecord, _add_months
from .services.common import _is_ajax, _operator
from .services.import_export import _parse_excel_sheets
from .services.license_query import annotate_status, status_aggregate


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
    """证照列表：仪表盘统计 + 搜索 + 状态筛选。

    状态与借出标记由 license_query 下推为数据库注解（审计 #10）：
    改造前 100 行 = 323 条 SQL（其中 312 次逐行 EXISTS），改造后固定 2 条。
    统计仍取全表（与改造前一致），不随筛选条件变化。
    """
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "")  # normal / expiring / expired

    annotated = annotate_status(License.objects.all())
    stats = status_aggregate(annotated)

    qs = annotated
    if q:
        qs = qs.filter(
            Q(name__icontains=q)
            | Q(code__icontains=q)
            | Q(holder__icontains=q)
            | Q(issuer__icontains=q)
        )
    if status:
        # 状态筛选同样在数据库完成，不再把整表读进 Python
        qs = qs.filter(_status=status)

    # 后端分页（10/30/50）：替代原先一次性把整表读进模板
    per_page = _per_page(request)
    page_obj = Paginator(qs, per_page).get_page(request.GET.get("page"))

    context = {
        "licenses": page_obj,
        "page_obj": page_obj,
        "per_page": per_page,
        "q": q,
        "status": status,
        "total": stats["total"],
        "expiring_count": stats["expiring_count"],
        "expired_count": stats["expired_count"],
        "borrowed_count": stats["borrowed_count"],
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
    """年审登记（AJAX 弹窗）。

    续期口径（审计 #24）：只有本次登记填了「新有效期」才推进 next_review_date。
    改造前无论是否变更有效期都会把下次年审日推到 review_date + cycle，
    结果是"有效期没变、年审计划却跳走"——不报错、也没人会发现的合规数据静默出错。
    未变更有效期时只登记、不动证照任何日期，并在页面上明确告知。
    """
    lic = get_object_or_404(License, pk=license_pk)
    form = AnnualReviewForm(request.POST or None)
    if form.is_valid():
        review = form.save(commit=False)
        review.license = lic
        review.save()
        renewed = False
        if review.result == "passed" and review.new_expire_date:
            lic.expire_date = review.new_expire_date
            if lic.review_cycle:
                lic.next_review_date = _add_months(review.review_date, lic.review_cycle)
            lic.save(update_fields=["expire_date", "next_review_date", "updated_at"])
            renewed = True
        if review.result == "passed" and not renewed:
            notice = "年审已登记；本次未填写新有效期，证照有效期与下次年审日均保持不变。"
        else:
            notice = "年审登记成功"
        _license_log(
            request, "年审", f"证照#{lic.id}",
            f"年审登记: {lic.code} - {review.get_result_display()}"
            + ("（续期至 %s）" % lic.expire_date if renewed else "（未变更有效期）"),
        )
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": notice, "renewed": renewed})
        messages.info(request, notice)
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
    per_page = _per_page(request)
    page_obj = Paginator(qs, per_page).get_page(request.GET.get("page"))
    return render(request, "archive/license_borrow_list.html", {
        "records": page_obj, "page_obj": page_obj, "per_page": per_page, "status": status,
    })


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
