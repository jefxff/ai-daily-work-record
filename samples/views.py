"""试样管理视图层（集成自独立 SEM 系统）。

写操作一律委托 services 层完成（编号/状态机/留痕），
视图只负责收集表单、分页筛选、异常提示。
全站已接入 RBAC：写操作按 samples 模块 C/U/D 权限控制；
登记人/操作人/审核人/上传人等人员字段一律由后端强制取当前登录用户姓名，
客户端提交值被忽略（防伪造），表单控件只读或隐藏。
"""

import logging
from datetime import datetime

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse

from accounts.decorators import perm_required
from accounts.services import get_display_name
from records.crud import make_list_view
from work_record.export_guard import guarded_export

from . import services, stats
from .exporters import export_samples_xlsx
from .filters import SampleFilter
from .forms import (
    AuditRecordForm,
    BatchRecordForm,
    PartnerForm,
    ProofAttachmentForm,
    SampleForm,
    TerminateForm,
    TestRecordForm,
)
from .models import (
    AuditRecord,
    BatchRecord,
    Partner,
    ProofAttachment,
    Sample,
    TestRecord,
)
from work_record.http_utils import is_ajax as _is_ajax  # 统一实现（复核报告 CODE-01）
from work_record.http_utils import parse_date as _shared_parse_date
from work_record.http_utils import per_page as _shared_per_page

logger = logging.getLogger(__name__)


def _per_page(request):
    """列表页每页条数：仅允许 10/30/50，默认 10（与全站一致）。"""
    return _shared_per_page(request, 10, choices=(10, 30, 50))



def _current_name(request):
    """当前登录用户显示姓名（自动带入人员字段，服务端唯一事实源）。"""
    return get_display_name(request.user)


def _lock_person_field(form, field_name, value):
    """人员字段只读化：控件加 readonly；绑定态以服务端值回显并覆盖客户端提交。"""
    form.fields[field_name].widget.attrs["readonly"] = True
    if form.is_bound:
        data = form.data.copy()
        data[field_name] = value or ""
        form.data = data


def _parse_date(value, default=None):
    return _shared_parse_date(value, default)


def _raise_to_form(form, exc):
    """把 services 层 full_clean 的 ValidationError 回填到表单。"""
    for field, errors in exc.message_dict.items():
        target = field if field in form.fields else None
        for error in errors:
            form.add_error(target, error)


def _modal_form(request, form, save_label, error_message="", ok=True):
    """渲染弹窗局部表单：GET 初始加载 ok=True(200)，POST 校验失败 ok=False(422)。"""
    html = render_to_string(
        "samples/_form_modal.html",
        {"form": form, "save_label": save_label, "error_message": error_message},
        request=request,
    )
    payload = {"ok": ok, "html": html}
    if error_message:
        payload["message"] = error_message
    return JsonResponse(payload, status=200 if ok else 422)


def _modal_error(message):
    """业务异常：仅返回错误消息（前端 toast，弹窗保留）。"""
    return JsonResponse({"ok": False, "message": str(message)}, status=422)


# 样品台账筛选状态在 session 中的键名
LIST_FILTER_SESSION_KEY = "samples_list_qs"


def _store_list_filter(request):
    """将当前样品台账的筛选条件（排除分页参数）保存到 session。

    用户在列表页执行筛选后，筛选状态被持久化；后续从详情页/编辑页点击
    「返回」时即可恢复到筛选后的结果列表，而非默认未筛选页面。
    排除 page 参数以避免返回到已不存在的页码。
    """
    params = request.GET.copy()
    params.pop("page", None)
    request.session[LIST_FILTER_SESSION_KEY] = params.urlencode()


def _list_qs(request):
    """读取 session 中保存的样品台账筛选查询串（供「返回」按钮恢复筛选）。"""
    return request.session.get(LIST_FILTER_SESSION_KEY, "")


# ---------------------------------------------------------------------------
# 统计看板
# ---------------------------------------------------------------------------

@perm_required("samples", "read")
def dashboard(request):
    range_key = request.GET.get("range", "month")
    if range_key in ("month", "quarter", "year", "all"):
        date_from, date_to = stats.preset_range(range_key)
    else:
        range_key = "custom"
        today = datetime.today().date()
        date_from = _parse_date(request.GET.get("date_from")) or today.replace(day=1)
        date_to = _parse_date(request.GET.get("date_to")) or today

    if date_from > date_to:
        messages.warning(request, "开始日期不能晚于结束日期，已默认展示本月数据。")
        date_from, date_to = stats.preset_range("month")
        range_key = "month"

    pass_basis = request.GET.get("basis", "imported")
    if pass_basis not in ("imported", "test"):
        pass_basis = "imported"

    data = stats.compute_dashboard(date_from, date_to, pass_basis)
    # 月度趋势条高度（最大 40px）
    month_max = max(data["month_values"]) if data["month_values"] else 0
    month_bars = [
        {"label": label, "value": value, "h": max(round(value / month_max * 38), 3) if month_max else 3}
        for label, value in zip(data["month_labels"], data["month_values"])
    ]
    # 状态分布（含百分比与配色，模板直接循环）
    badge_map = {"pending": "#bfbfbf", "testing": "#1890ff", "small_batch": "#fa8c16",
                 "imported": "#52c41a", "terminated": "#f5222d"}
    status_dist = [
        {
            "key": key,
            "label": label,
            "count": data["status_counts"].get(key, 0),
            "pct": round(data["status_counts"].get(key, 0) / data["total"] * 100, 1) if data["total"] else 0,
            "color": badge_map.get(key, "#bfbfbf"),
        }
        for key, label in Sample.Status.choices
    ]
    # 供应商 Top10（含百分比，模板直接循环）
    supplier_max = data["supplier_values"][0] if data["supplier_values"] else 0
    supplier_bars = [
        {
            "name": name,
            "count": count,
            "pct": round(count / supplier_max * 100) if supplier_max else 0,
        }
        for name, count in zip(data["supplier_labels"], data["supplier_values"])
    ]
    context = {
        "range_key": range_key,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "basis": pass_basis,
        "data": data,
        "month_bars": month_bars,
        "status_dist": status_dist,
        "supplier_bars": supplier_bars,
        "status_choices": Sample.Status.choices,
    }
    return render(request, "samples/dashboard.html", context)


# ---------------------------------------------------------------------------
# 样品主档台账
# ---------------------------------------------------------------------------

@perm_required("samples", "read")
def sample_list(request):
    flt = SampleFilter(request.GET)
    qs = flt.apply().select_related("supplier")
    page_no = request.GET.get("page") or 1
    page_obj = Paginator(qs, _per_page(request)).get_page(page_no)

    # 保存当前筛选状态，供详情/编辑页「返回」时恢复到筛选后的结果列表
    _store_list_filter(request)

    # 登记人筛选项：取台账中已使用的登记人姓名（开放填写，无独立用户表）
    registrars = (
        Sample.objects.exclude(registrar="")
        .values_list("registrar", flat=True)
        .distinct()
        .order_by("registrar")
    )
    context = {
        "page_obj": page_obj,
        "flt": flt,
        "query_string": flt.query_string(),
        "suppliers": Partner.objects.order_by("name"),
        "status_choices": Sample.Status.choices,
        "registrars": registrars,
    }
    return render(request, "samples/list.html", context)


@perm_required("samples", "create")
def sample_create(request):
    registrar = _current_name(request)
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
    error_message = ""
    if request.method == "POST":
        form = SampleForm(request.POST)
        _lock_person_field(form, "registrar", registrar)
        if form.is_valid():
            try:
                unsaved = form.save(commit=False)
                unsaved.registrar = registrar
                sample = services.create_sample(
                    unsaved,
                    registrar,
                    services.client_ip(request),
                )
                messages.success(request, f"样品 {sample.sample_no} 登记成功，当前状态：待送样。")
                if is_ajax:
                    return JsonResponse({"ok": True, "message": f"样品 {sample.sample_no} 登记成功"})
                return redirect("samples:detail", pk=sample.pk)
            except ValidationError as exc:
                _raise_to_form(form, exc)
            except services.BusinessError as exc:
                error_message = str(exc)
                messages.error(request, error_message)
        if is_ajax:
            html = render_to_string(
                "samples/_form_modal.html",
                {"form": form, "save_label": "保存", "error_message": error_message},
                request=request,
            )
            return JsonResponse({"ok": False, "html": html}, status=422)
    else:
        form = SampleForm(initial={"registrar": registrar})
        _lock_person_field(form, "registrar", registrar)
    if is_ajax:
        html = render_to_string(
            "samples/_form_modal.html",
            {"form": form, "save_label": "保存", "error_message": ""},
            request=request,
        )
        return JsonResponse({"ok": True, "html": html})
    return render(
        request,
        "samples/form.html",
        {"form": form, "title": "送样登记", "list_qs": _list_qs(request)},
    )


@perm_required("samples", "update")
def sample_edit(request, pk):
    sample = get_object_or_404(Sample.objects.select_related("registrar", "handler"), pk=pk)
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
    if sample.is_deleted:
        messages.error(request, "样品已删除，请先恢复后再编辑。")
        if is_ajax:
            return JsonResponse({"ok": False, "message": "样品已删除，请先恢复后再编辑。"}, status=422)
        return redirect("samples:detail", pk=pk)
    # 登记人为历史事实：编辑时只读展示且保持原值，不允许修改
    registrar = sample.registrar
    error_message = ""
    if request.method == "POST":
        form = SampleForm(request.POST, instance=sample)
        _lock_person_field(form, "registrar", registrar)
        if form.is_valid():
            try:
                services.update_sample(
                    form.save(commit=False),
                    registrar,
                    services.client_ip(request),
                )
                messages.success(request, "样品信息已更新。")
                if is_ajax:
                    return JsonResponse({"ok": True, "message": "样品信息已更新"})
                return redirect("samples:detail", pk=pk)
            except ValidationError as exc:
                _raise_to_form(form, exc)
            except services.BusinessError as exc:
                error_message = str(exc)
                messages.error(request, error_message)
        if is_ajax:
            html = render_to_string(
                "samples/_form_modal.html",
                {"form": form, "save_label": "保存修改", "error_message": error_message},
                request=request,
            )
            return JsonResponse({"ok": False, "html": html}, status=422)
    else:
        form = SampleForm(instance=sample, initial={"registrar": registrar})
        _lock_person_field(form, "registrar", registrar)
    if is_ajax:
        html = render_to_string(
            "samples/_form_modal.html",
            {"form": form, "save_label": "保存修改", "error_message": ""},
            request=request,
        )
        return JsonResponse({"ok": True, "html": html})
    return render(
        request,
        "samples/form.html",
        {
            "form": form,
            "title": f"编辑样品 {sample.sample_no}",
            "list_qs": _list_qs(request),
        },
    )


@perm_required("samples", "read")
def sample_detail(request, pk):
    sample = get_object_or_404(
        Sample.objects.select_related("supplier"),
        pk=pk,
    )
    test_records = list(sample.test_records.order_by("round_no"))
    batch_records = list(sample.batch_records.order_by("round_no"))
    # 审厂记录按供应商维度管理，所有该供应商的样品共享
    audit_records = list(
        sample.supplier.audit_records.order_by("round_no")
    ) if sample.supplier else []

    # 证明材料：按 6 个业务阶段分组，便于详情页渲染阶段卡片
    proofs_qs = sample.proof_attachments.order_by("-created_at")
    proofs_by_stage = {}
    for stage_value, _ in ProofAttachment.Stage.choices:
        proofs_by_stage[stage_value] = [p for p in proofs_qs if p.stage == stage_value]
    # 阶段汇总（用于步骤进度提示：已上传 vs 总阶段数）
    completed_stage_count = sum(1 for v in proofs_by_stage.values() if v)

    # 阶段元数据列表：每项含编号/值/标签/徽章色/说明/图标/附件，供模板直接循环渲染
    proof_stage_meta = []
    for idx, (stage_value, stage_label) in enumerate(ProofAttachment.Stage.choices, start=1):
        proof_stage_meta.append(
            {
                "num": idx,
                "value": stage_value,
                "label": stage_label,
                "badge": ProofAttachment.STAGE_BADGE.get(stage_value, "default"),
                "desc": ProofAttachment.STAGE_DESC.get(stage_value, ""),
                "icon": ProofAttachment.STAGE_ICON.get(stage_value, "📎"),
                "attachments": proofs_by_stage.get(stage_value, []),
            }
        )

    # 时间轴：将证明材料节点按业务逻辑顺序映射插入，保证与卡片 1→6 顺序一致
    timeline = _build_timeline(sample, test_records, batch_records, proof_stage_meta, audit_records)

    # 流程步骤条当前位置；终止样品按实际到达过的阶段回推
    if sample.status == Sample.Status.TERMINATED:
        if batch_records:
            status_index = 2
        elif test_records:
            status_index = 1
        else:
            status_index = 0
    else:
        status_index = {
            Sample.Status.PENDING: 0,
            Sample.Status.TESTING: 1,
            Sample.Status.SMALL_BATCH: 2,
            Sample.Status.IMPORTED: 3,
        }.get(sample.status, 0)

    context = {
        "sample": sample,
        "test_records": test_records,
        "batch_records": batch_records,
        "audit_records": audit_records,
        "timeline": timeline,
        "status_index": status_index,
        "can_add_test": sample.status
        in (Sample.Status.PENDING, Sample.Status.TESTING)
        and not sample.is_deleted,
        "can_add_batch": sample.status == Sample.Status.SMALL_BATCH and not sample.is_deleted,
        "can_add_audit": sample.supplier_type == Sample.SupplierType.NEW and not sample.is_deleted,
        "can_terminate": not sample.is_terminal and not sample.is_deleted,
        "proof_stages": proof_stage_meta,
        "proof_completed": completed_stage_count,
        "proof_total": len(ProofAttachment.Stage.choices),
        "can_upload_proof": not sample.is_deleted,
        "list_qs": _list_qs(request),
    }
    return render(request, "samples/detail.html", context)


# 证明材料阶段在时间轴上的逻辑位置（整数，基准扩大 1000 避免浮点精度问题）：
# 试样通过后→价目表/通知/小批量订单（试样与小批量之间）→小批量结果（小批量后）→量产订单（量产后）
PROOF_TIMELINE_POS = {
    "test_report": 200000,
    "price_list_update": 201000,
    "small_batch_notification": 202000,
    "small_batch_order": 203000,
    "small_batch_result": 399000,
    "mass_production_order": 401000,
}


def _build_timeline(sample, test_records, batch_records, proof_stages=None, audit_records=None):
    """构造详情时间轴事件（按业务逻辑顺序排列）。

    证明材料节点按阶段逻辑顺序插入到对应业务阶段之间，保证时间轴顺序与
    卡片逻辑顺序（1→6）一致；每个已上传凭证的阶段生成一个节点，展示
    缩略图/上传人/时间等关键信息。
    """
    events = [
        {
            "pos": 0,
            "time": sample.registered_at,
            "color": "default",
            "icon": "📦",
            "title": "送样登记",
            "lines": [
                f"登记人：{sample.registrar or '—'}",
                f"供应商：{sample.supplier.name if sample.supplier else '未指定'}",
                f"送样时间：{sample.sample_date:%Y-%m-%d}",
            ],
        }
    ]
    for record in test_records:
        color = record.conclusion_badge
        lines = [
            f"测试结果：{record.test_result}",
            f"评审结论：{record.get_review_conclusion_display()}",
            f"报告编码：{record.report_code}",
            f"已通知责任人：{'是' if record.notified else '否'}",
            f"操作人：{record.operator or '—'}",
        ]
        if record.remark:
            lines.append(f"备注：{record.remark}")
        events.append(
            {
                "pos": 100000 + record.round_no,
                "time": record.operated_at,
                "color": color,
                "icon": "🧪",
                "title": f"第{record.round_no}轮试样（{record.test_no}）",
                "lines": lines,
            }
        )
    for record in batch_records:
        color = record.result_badge
        lines = [
            f"是否下单：{'是' if record.ordered else '否'}",
            f"小批量结果：{record.get_batch_result_display()}",
            f"是否录入金蝶：{'是' if record.kingdee_entered else '否'}",
            f"操作人：{record.operator or '—'}",
        ]
        if record.unit_price is not None:
            lines.append(f"试样单价：{record.unit_price} 元")
        if record.alternative_model:
            lines.append(f"可替代型号：{record.alternative_model}")
        if record.remark:
            lines.append(f"备注：{record.remark}")
        events.append(
            {
                "pos": 300000 + record.round_no,
                "time": record.operated_at,
                "color": color,
                "icon": "🏭",
                "title": f"第{record.round_no}批小批量验证（{record.batch_no}）",
                "lines": lines,
            }
        )
    if sample.imported_at:
        events.append(
            {
                "pos": 400000,
                "time": sample.imported_at,
                "color": "success",
                "icon": "✅",
                "title": "正式量产导入",
                "lines": [f"确认人：{sample.imported_by or '—'}"],
            }
        )
    if sample.terminated_at:
        events.append(
            {
                "pos": 500000,
                "time": sample.terminated_at,
                "color": "danger",
                "icon": "⛔",
                "title": "试样终止",
                "lines": [
                    f"操作人：{sample.terminated_by or '—'}",
                    f"终止原因：{sample.terminate_reason}",
                ],
            }
        )

    # 新供应商审厂节点：仅 supplier_type == new 时出现
    # 每轮审厂记录生成一个节点，位于价目表更新(201)之后、小批量通知(202)之前
    # 无审厂记录时生成待上传占位节点
    if sample.supplier_type == Sample.SupplierType.NEW:
        if audit_records:
            for record in audit_records:
                color = record.result_badge
                lines = [
                    f"供应商名称：{sample.supplier.name if sample.supplier else '—'}",
                    f"审核日期：{record.audit_date:%Y-%m-%d}",
                    f"审核结果：{record.get_audit_result_display()}",
                    f"操作人：{record.auditor or '—'}",
                    f"审厂轮次：第{record.round_no}轮",
                ]
                if record.audit_remark:
                    lines.append(f"审厂备注：{record.audit_remark}")
                if record.file:
                    lines.append(f"审核文件：{record.file.name.split('/')[-1]}")
                events.append(
                    {
                        "pos": 201500 + record.round_no,
                        "time": record.operated_at,
                        "color": color,
                        "icon": "🏭",
                        "title": f"第{record.round_no}轮审厂（{record.audit_no}）",
                        "lines": lines,
                        "is_audit": True,
                        "audit_round": record.round_no,
                    }
                )
        else:
            # 新增供应商但尚未审厂：占位节点
            events.append(
                {
                    "pos": 201500,
                    "time": None,
                    "color": "light",
                    "icon": "🏭",
                    "title": "新供应商审厂情况（待审核）",
                    "lines": [
                        f"供应商名称：{sample.supplier.name if sample.supplier else '—'}",
                        "状态：待审厂",
                    ],
                    "is_audit": True,
                    "audit_pending": True,
                }
            )

    # 证明材料节点：所有6个阶段都生成时间轴节点（未上传显示为待上传占位），
    # 按逻辑位置插入对应业务阶段之间，保证时间轴顺序与卡片 1→6 顺序一致
    if proof_stages:
        stage_map = {s["value"]: s for s in proof_stages}
        for stage_value, pos in PROOF_TIMELINE_POS.items():
            stage = stage_map.get(stage_value)
            if not stage:
                continue
            attachments = stage.get("attachments") or []
            if attachments:
                # 已上传：彩色节点 + 缩略图 + 文件数 + 上传人
                latest = max(attachments, key=lambda a: a.created_at)
                thumbs = []
                uploaders = set()
                remarks = []
                for att in attachments:
                    thumbs.append(
                        {
                            "url": att.file.url,
                            "name": att.original_name,
                            "is_image": att.is_image,
                        }
                    )
                    if att.uploaded_by:
                        uploaders.add(att.uploaded_by)
                    if att.remark:
                        remarks.append(att.remark)
                node_lines = [
                    f"凭证阶段：{stage['label']}",
                    f"文件数：{len(attachments)} 个",
                    f"上传人：{'、'.join(sorted(uploaders)) if uploaders else '—'}",
                ]
                if remarks:
                    node_lines.append("备注：" + " / ".join(remarks))
                events.append(
                    {
                        "pos": pos,
                        "time": latest.created_at,
                        "color": stage["badge"],
                        "icon": stage["icon"],
                        "title": f"凭证·{stage['label']}",
                        "lines": node_lines,
                        "is_proof": True,
                        "proof_stage": stage_value,
                        "proof_thumbs": thumbs,
                        "proof_count": len(attachments),
                        "proof_pending": False,
                    }
                )
            else:
                # 未上传：灰色占位节点，提示待上传，点击跳转到卡片上传表单
                events.append(
                    {
                        "pos": pos,
                        "time": None,
                        "color": "light",
                        "icon": stage["icon"],
                        "title": f"凭证·{stage['label']}（待上传）",
                        "lines": [
                            f"凭证阶段：{stage['label']}",
                            stage.get("desc", "") or "尚未上传凭证",
                            "状态：待上传",
                        ],
                        "is_proof": True,
                        "proof_stage": stage_value,
                        "proof_thumbs": [],
                        "proof_count": 0,
                        "proof_pending": True,
                    }
                )

    events.sort(key=lambda e: e["pos"])
    return events


@perm_required("samples", "delete")
def sample_delete(request, pk):
    sample = get_object_or_404(Sample.objects.select_related("registrar", "handler"), pk=pk)
    if request.method == "POST":
        try:
            services.soft_delete_sample(sample, _current_name(request), services.client_ip(request))
            messages.success(request, f"样品 {sample.sample_no} 已删除（可在回收站查看恢复）。")
        except services.BusinessError as exc:
            messages.error(request, str(exc))
        return redirect("samples:list")
    return render(request, "samples/confirm_delete.html", {"sample": sample, "list_qs": _list_qs(request)})


@perm_required("samples", "update")
def sample_restore(request, pk):
    sample = get_object_or_404(Sample.objects.select_related("registrar", "handler"), pk=pk)
    if request.method == "POST":
        try:
            services.restore_sample(sample, _current_name(request), services.client_ip(request))
            messages.success(request, f"样品 {sample.sample_no} 已恢复。")
        except services.BusinessError as exc:
            messages.error(request, str(exc))
    return redirect("samples:detail", pk=pk)


@perm_required("samples", "update")
def sample_terminate(request, pk):
    sample = get_object_or_404(Sample.objects.select_related("registrar", "handler"), pk=pk)
    operator = _current_name(request)
    if request.method == "POST":
        form = TerminateForm(request.POST)
        form.fields["operator"].widget = forms.HiddenInput()
        if form.is_valid():
            try:
                services.terminate_sample(
                    sample,
                    operator,
                    form.cleaned_data["reason"],
                    services.client_ip(request),
                )
                messages.success(request, f"样品 {sample.sample_no} 已终止。")
                return redirect("samples:detail", pk=pk)
            except services.BusinessError as exc:
                messages.error(request, str(exc))
    else:
        form = TerminateForm()
        form.fields["operator"].widget = forms.HiddenInput()
    return render(request, "samples/terminate_form.html", {"form": form, "sample": sample, "operator_name": operator, "list_qs": _list_qs(request)})


# ---------------------------------------------------------------------------
# 试样记录
# ---------------------------------------------------------------------------

@perm_required("samples", "create")
def test_create(request, pk):
    sample = get_object_or_404(Sample.objects.select_related("registrar", "handler"), pk=pk)
    operator = _current_name(request)
    is_ajax = _is_ajax(request)
    if request.method == "POST":
        form = TestRecordForm(request.POST)
        _lock_person_field(form, "operator", operator)
        if form.is_valid():
            record = form.save(commit=False)
            record.sample = sample
            record.operator = operator
            try:
                services.create_test_record(
                    record,
                    operator,
                    services.client_ip(request),
                    terminate_reason=form.cleaned_data.get("terminate_reason", ""),
                )
                messages.success(
                    request,
                    f"第{record.round_no}轮试样记录已保存，样品状态更新为「{sample.get_status_display()}」。",
                )
                if is_ajax:
                    return JsonResponse({"ok": True, "message": f"第{record.round_no}轮试样记录已保存"})
                return redirect("samples:detail", pk=pk)
            except ValidationError as exc:
                _raise_to_form(form, exc)
            except services.BusinessError as exc:
                messages.error(request, str(exc))
                if is_ajax:
                    return _modal_error(exc)
                return redirect("samples:detail", pk=pk)
        if is_ajax:
            return _modal_form(request, form, "保存", ok=False)
    else:
        form = TestRecordForm(initial={"operator": operator})
        _lock_person_field(form, "operator", operator)
    if is_ajax:
        return _modal_form(request, form, "保存")
    return render(
        request,
        "samples/test_form.html",
        {"form": form, "sample": sample, "next_round": sample.latest_test_round + 1},
    )


@perm_required("samples", "update")
def test_edit(request, pk, record_pk):
    sample = get_object_or_404(Sample.objects.select_related("registrar", "handler"), pk=pk)
    record = get_object_or_404(TestRecord, pk=record_pk, sample=sample)
    # 操作人为历史事实：保持原值，不允许借编辑篡改
    operator = record.operator
    is_ajax = _is_ajax(request)
    error_message = ""
    if request.method == "POST":
        form = TestRecordForm(request.POST, instance=record)
        _lock_person_field(form, "operator", operator)
        if form.is_valid():
            try:
                services.update_test_record(
                    form.save(commit=False),
                    operator,
                    services.client_ip(request),
                )
                messages.success(request, "试样记录已更新。")
                if is_ajax:
                    return JsonResponse({"ok": True, "message": "试样记录已更新"})
                return redirect("samples:detail", pk=pk)
            except ValidationError as exc:
                _raise_to_form(form, exc)
            except services.BusinessError as exc:
                error_message = str(exc)
                messages.error(request, error_message)
        if is_ajax:
            return _modal_form(request, form, "保存修改", error_message, ok=False)
    else:
        form = TestRecordForm(instance=record)
        _lock_person_field(form, "operator", operator)
    if is_ajax:
        return _modal_form(request, form, "保存修改")
    return render(
        request,
        "samples/test_form.html",
        {"form": form, "sample": sample, "record": record, "next_round": record.round_no},
    )


@perm_required("samples", "delete")
def test_delete(request, pk, record_pk):
    sample = get_object_or_404(Sample.objects.select_related("registrar", "handler"), pk=pk)
    record = get_object_or_404(TestRecord, pk=record_pk, sample=sample)
    if request.method == "POST":
        try:
            services.delete_test_record(record, _current_name(request), services.client_ip(request))
            messages.success(request, "试样记录已删除。")
        except services.BusinessError as exc:
            messages.error(request, str(exc))
        return redirect("samples:detail", pk=pk)
    return render(
        request,
        "samples/confirm_delete_record.html",
        {"sample": sample, "record": record, "kind": "试样记录", "record_no": record.test_no},
    )


# ---------------------------------------------------------------------------
# 小批量记录
# ---------------------------------------------------------------------------

@perm_required("samples", "create")
def batch_create(request, pk):
    sample = get_object_or_404(Sample.objects.select_related("registrar", "handler"), pk=pk)
    operator = _current_name(request)
    is_ajax = _is_ajax(request)
    if request.method == "POST":
        form = BatchRecordForm(request.POST)
        _lock_person_field(form, "operator", operator)
        if form.is_valid():
            record = form.save(commit=False)
            record.sample = sample
            record.operator = operator
            try:
                services.create_batch_record(
                    record,
                    operator,
                    services.client_ip(request),
                    terminate_reason=form.cleaned_data.get("terminate_reason", ""),
                )
                messages.success(
                    request,
                    f"第{record.round_no}批小批量记录已保存，样品状态更新为「{sample.get_status_display()}」。",
                )
                if is_ajax:
                    return JsonResponse({"ok": True, "message": f"第{record.round_no}批小批量记录已保存"})
                return redirect("samples:detail", pk=pk)
            except ValidationError as exc:
                _raise_to_form(form, exc)
            except services.BusinessError as exc:
                messages.error(request, str(exc))
                if is_ajax:
                    return _modal_error(exc)
                return redirect("samples:detail", pk=pk)
        if is_ajax:
            return _modal_form(request, form, "保存", ok=False)
    else:
        form = BatchRecordForm(initial={"operator": operator})
        _lock_person_field(form, "operator", operator)
    if is_ajax:
        return _modal_form(request, form, "保存")
    return render(
        request,
        "samples/batch_form.html",
        {"form": form, "sample": sample, "next_round": sample.latest_batch_round + 1},
    )


@perm_required("samples", "update")
def batch_edit(request, pk, record_pk):
    sample = get_object_or_404(Sample.objects.select_related("registrar", "handler"), pk=pk)
    record = get_object_or_404(BatchRecord, pk=record_pk, sample=sample)
    # 操作人为历史事实：保持原值，不允许借编辑篡改
    operator = record.operator
    is_ajax = _is_ajax(request)
    error_message = ""
    if request.method == "POST":
        form = BatchRecordForm(request.POST, instance=record)
        _lock_person_field(form, "operator", operator)
        if form.is_valid():
            try:
                services.update_batch_record(
                    form.save(commit=False),
                    operator,
                    services.client_ip(request),
                )
                messages.success(request, "小批量记录已更新。")
                if is_ajax:
                    return JsonResponse({"ok": True, "message": "小批量记录已更新"})
                return redirect("samples:detail", pk=pk)
            except ValidationError as exc:
                _raise_to_form(form, exc)
            except services.BusinessError as exc:
                error_message = str(exc)
                messages.error(request, error_message)
        if is_ajax:
            return _modal_form(request, form, "保存修改", error_message, ok=False)
    else:
        form = BatchRecordForm(instance=record)
        _lock_person_field(form, "operator", operator)
    if is_ajax:
        return _modal_form(request, form, "保存修改")
    return render(
        request,
        "samples/batch_form.html",
        {"form": form, "sample": sample, "record": record, "next_round": record.round_no},
    )


@perm_required("samples", "delete")
def batch_delete(request, pk, record_pk):
    sample = get_object_or_404(Sample.objects.select_related("registrar", "handler"), pk=pk)
    record = get_object_or_404(BatchRecord, pk=record_pk, sample=sample)
    if request.method == "POST":
        try:
            services.delete_batch_record(record, _current_name(request), services.client_ip(request))
            messages.success(request, "小批量记录已删除。")
        except services.BusinessError as exc:
            messages.error(request, str(exc))
        return redirect("samples:detail", pk=pk)
    return render(
        request,
        "samples/confirm_delete_record.html",
        {"sample": sample, "record": record, "kind": "小批量记录", "record_no": record.batch_no},
    )


# ---------------------------------------------------------------------------
# 供应商类型切换（AJAX）
# ---------------------------------------------------------------------------

@perm_required("samples", "update")
def supplier_type_update(request, pk):
    """AJAX 更新样品的供应商类型。"""
    if request.method != "POST":
        return JsonResponse({"ok": False, "message": "仅支持 POST"}, status=405)
    sample = get_object_or_404(Sample.objects.select_related("registrar", "handler"), pk=pk)
    new_type = (request.POST.get("supplier_type") or "").strip()
    if new_type not in dict(Sample.SupplierType.choices):
        return JsonResponse({"ok": False, "message": "无效的供应商类型"},
                            status=422)
    old_type = sample.supplier_type
    sample.supplier_type = new_type
    sample.save(update_fields=["supplier_type"])
    # 留痕（统一写入系统操作留痕）
    services._log(
        _current_name(request),
        services.Action.UPDATE,
        "样品主档",
        sample.sample_no,
        f"供应商类型 {old_type} → {new_type}",
        services.client_ip(request),
    )
    return JsonResponse({"ok": True, "supplier_type": new_type})


# ---------------------------------------------------------------------------
# 审厂记录（多轮现场审核，按供应商维度管理）
# ---------------------------------------------------------------------------

@perm_required("samples", "create")
def audit_create(request, pk):
    """新增审厂记录（pk 为供应商 Partner.pk）。"""
    partner = get_object_or_404(Partner, pk=pk)
    auditor = _current_name(request)
    is_ajax = _is_ajax(request)
    if request.method == "POST":
        form = AuditRecordForm(request.POST, request.FILES)
        _lock_person_field(form, "auditor", auditor)
        if form.is_valid():
            record = form.save(commit=False)
            record.partner = partner
            record.auditor = auditor

            def _assign_and_save():
                next_round = (partner.audit_records.first().round_no if partner.audit_records.exists() else 0) + 1
                record.round_no = next_round
                record.audit_no = f"A{partner.partner_code}-{next_round:02d}" if partner.partner_code else ""
                record.save()
                return next_round

            next_round = services.save_with_retry(_assign_and_save)
            services._log(
                auditor,
                services.Action.CREATE,
                "审厂记录",
                record.audit_no,
                f"{partner.name} 新增第{next_round}轮审厂记录",
                services.client_ip(request),
            )
            # 通知触发：审厂结果登记后通知有试样读取权限的用户（业务提醒，同轮次幂等）
            try:
                from notifications.services import emit, users_with_perm
                for notify_user in users_with_perm("samples", "read"):
                    emit(
                        "sample_audit_result",
                        recipient=notify_user,
                        context={
                            "partner_name": partner.name,
                            "round_no": next_round,
                            "audit_result": record.get_audit_result_display(),
                            "audit_no": record.audit_no,
                        },
                        dedup_context=f"audit:{record.pk}:{notify_user.pk}",
                        link=f"/samples/partners/{partner.pk}/",
                    )
            except Exception:
                # 通知失败不影响业务主流程，但必须留痕以便事后排查
                logger.exception("审厂记录保存后发送通知失败")
            messages.success(request, f"第{next_round}轮审厂记录已保存。")
            if is_ajax:
                return JsonResponse({"ok": True, "message": f"第{next_round}轮审厂记录已保存"})
            return redirect("samples:partner_detail", pk=pk)
        if is_ajax:
            return _modal_form(request, form, "保存", ok=False)
    else:
        form = AuditRecordForm(initial={"auditor": auditor})
        _lock_person_field(form, "auditor", auditor)
    if is_ajax:
        return _modal_form(request, form, "保存")
    next_round = (partner.audit_records.first().round_no if partner.audit_records.exists() else 0) + 1
    return render(
        request,
        "samples/audit_form.html",
        {"form": form, "partner": partner, "next_round": next_round},
    )


@perm_required("samples", "update")
def audit_edit(request, pk, record_pk):
    """编辑审厂记录（pk 为供应商 Partner.pk）。"""
    partner = get_object_or_404(Partner, pk=pk)
    record = get_object_or_404(AuditRecord, pk=record_pk, partner=partner)
    # 审核人为历史事实：保持原值，不允许借编辑篡改
    auditor = record.auditor
    is_ajax = _is_ajax(request)
    if request.method == "POST":
        form = AuditRecordForm(request.POST, request.FILES, instance=record)
        _lock_person_field(form, "auditor", auditor)
        if form.is_valid():
            record = form.save(commit=False)
            record.auditor = auditor
            record.save()
            services._log(
                auditor,
                services.Action.UPDATE,
                "审厂记录",
                record.audit_no,
                f"{partner.name} 编辑第{record.round_no}轮审厂记录",
                services.client_ip(request),
            )
            messages.success(request, "审厂记录已更新。")
            if is_ajax:
                return JsonResponse({"ok": True, "message": "审厂记录已更新"})
            return redirect("samples:partner_detail", pk=pk)
        if is_ajax:
            return _modal_form(request, form, "保存修改", ok=False)
    else:
        form = AuditRecordForm(instance=record)
        _lock_person_field(form, "auditor", auditor)
    if is_ajax:
        return _modal_form(request, form, "保存修改")
    return render(
        request,
        "samples/audit_form.html",
        {"form": form, "partner": partner, "record": record, "next_round": record.round_no},
    )


@perm_required("samples", "delete")
def audit_delete(request, pk, record_pk):
    """删除审厂记录（pk 为供应商 Partner.pk）。"""
    partner = get_object_or_404(Partner, pk=pk)
    record = get_object_or_404(AuditRecord, pk=record_pk, partner=partner)
    if request.method == "POST":
        round_no = record.round_no
        audit_no = record.audit_no
        record.delete()
        # 重排轮次号，保证连续（1,2,3...）
        remaining = AuditRecord.objects.filter(partner=partner).order_by("round_no")
        for new_round, rec in enumerate(remaining, start=1):
            if rec.round_no != new_round:
                rec.round_no = new_round
                # 同步更新编号中的轮次后缀
                if rec.audit_no:
                    rec.audit_no = f"A{partner.partner_code}-{new_round:02d}"
                rec.save(update_fields=["round_no", "audit_no"])
        services._log(
            _current_name(request),
            services.Action.DELETE,
            "审厂记录",
            audit_no,
            f"{partner.name} 删除第{round_no}轮审厂记录",
            services.client_ip(request),
        )
        messages.success(request, "审厂记录已删除。")
        return redirect("samples:partner_detail", pk=pk)
    return render(
        request,
        "samples/confirm_delete_record.html",
        {"partner": partner, "record": record, "kind": "审厂记录", "record_no": record.audit_no},
    )


# ---------------------------------------------------------------------------
# 证明材料（业务流程关键节点凭证）
# ---------------------------------------------------------------------------

@perm_required("samples", "create")
def proof_upload(request, pk, stage):
    """上传指定业务阶段的证明材料。

    采用 POST 提交后重定向回详情页证明材料区段（#proofs），
    保持 redirect-after-POST 模式；表单错误同样回详情页以 messages 提示。
    上传人由后端强制取当前登录用户，客户端提交值忽略。
    """
    sample = get_object_or_404(Sample.objects.select_related("registrar", "handler"), pk=pk)
    if stage not in dict(ProofAttachment.Stage.choices):
        messages.error(request, "无效的证明材料阶段。")
        return redirect("samples:detail", pk=pk)
    uploaded_by = _current_name(request)
    if request.method == "POST":
        form = ProofAttachmentForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                services.upload_proof(
                    sample,
                    stage,
                    form.cleaned_data["file"],
                    uploaded_by,
                    services.client_ip(request),
                    remark=form.cleaned_data.get("remark", ""),
                )
                messages.success(
                    request,
                    f"证明材料「{dict(ProofAttachment.Stage.choices)[stage]}」上传成功。",
                )
            except services.BusinessError as exc:
                messages.error(request, str(exc))
        else:
            for field_errors in form.errors.values():
                for err in field_errors:
                    messages.error(request, err)
    return redirect(reverse("samples:detail", kwargs={"pk": pk}) + "#proofs")


@perm_required("samples", "delete")
def proof_delete(request, pk, attachment_pk):
    """删除证明材料附件（仅 POST，详情页内联表单带确认）。"""
    sample = get_object_or_404(Sample.objects.select_related("registrar", "handler"), pk=pk)
    attachment = get_object_or_404(ProofAttachment, pk=attachment_pk, sample=sample)
    if request.method == "POST":
        try:
            services.delete_proof(attachment, _current_name(request), services.client_ip(request))
            messages.success(request, "证明材料已删除。")
        except services.BusinessError as exc:
            messages.error(request, str(exc))
    return redirect(reverse("samples:detail", kwargs={"pk": pk}) + "#proofs")


# ---------------------------------------------------------------------------
# 供应商档案
# ---------------------------------------------------------------------------

# 供应商档案列表（keyword 搜索去首尾空白；非法状态值忽略不筛选）
partner_list = make_list_view(
    perm_module="samples", template_name="samples/partner_list.html",
    page_var="page_obj", model=Partner,
    search_param="keyword", search_fields=["name", "partner_code", "contact_person"],
    filters=[
        ("status",
         lambda qs, v: qs.filter(status=v) if v in dict(Partner.CooperationStatus.choices) else qs,
         "strip", "status"),
    ],
    extra_context={"status_choices": Partner.CooperationStatus.choices},
)


@perm_required("samples", "create")
def partner_create(request):
    is_ajax = _is_ajax(request)
    error_message = ""
    if request.method == "POST":
        form = PartnerForm(request.POST)
        if form.is_valid():
            try:
                partner = services.create_partner(
                    form.save(commit=False), _current_name(request), services.client_ip(request)
                )
                messages.success(request, f"档案 {partner.partner_code} 创建成功。")
                if is_ajax:
                    return JsonResponse({"ok": True, "message": f"档案 {partner.partner_code} 创建成功"})
                return redirect("samples:partner_list")
            except ValidationError as exc:
                _raise_to_form(form, exc)
            except services.BusinessError as exc:
                error_message = str(exc)
                messages.error(request, error_message)
        if is_ajax:
            return _modal_form(request, form, "保存", error_message, ok=False)
    else:
        form = PartnerForm()
    if is_ajax:
        return _modal_form(request, form, "保存")
    return render(request, "samples/partner_form.html", {"form": form, "title": "新增供应商档案"})


@perm_required("samples", "read")
def partner_detail(request, pk):
    """供应商档案详情页（含审厂记录）。"""
    partner = get_object_or_404(Partner, pk=pk)
    audit_records = list(partner.audit_records.order_by("round_no"))
    # 该供应商关联的样品（分页）
    samples_qs = partner.supplier_samples.order_by("-created_at")
    samples_count = samples_qs.count()
    page_obj = Paginator(samples_qs, _per_page(request)).get_page(request.GET.get("page") or 1)
    next_audit_round = (audit_records[0].round_no if audit_records else 0) + 1
    context = {
        "partner": partner,
        "audit_records": audit_records,
        "samples": page_obj,
        "next_audit_round": next_audit_round,
    }
    if _is_ajax(request):
        context["samples_count"] = samples_count
        context["samples_list"] = list(samples_qs[:30])
        html = render_to_string("samples/_partner_detail_modal.html", context, request=request)
        return JsonResponse({"ok": True, "html": html})
    return render(request, "samples/partner_detail.html", context)


@perm_required("samples", "update")
def partner_edit(request, pk):
    partner = get_object_or_404(Partner, pk=pk)
    is_ajax = _is_ajax(request)
    error_message = ""
    if request.method == "POST":
        form = PartnerForm(request.POST, instance=partner)
        if form.is_valid():
            try:
                services.update_partner(
                    form.save(commit=False), _current_name(request), services.client_ip(request)
                )
                messages.success(request, "档案已更新。")
                if is_ajax:
                    return JsonResponse({"ok": True, "message": "档案已更新"})
                return redirect("samples:partner_list")
            except ValidationError as exc:
                _raise_to_form(form, exc)
            except services.BusinessError as exc:
                error_message = str(exc)
                messages.error(request, error_message)
        if is_ajax:
            return _modal_form(request, form, "保存修改", error_message, ok=False)
    else:
        form = PartnerForm(instance=partner)
    if is_ajax:
        return _modal_form(request, form, "保存修改")
    return render(
        request, "samples/partner_form.html", {"form": form, "title": f"编辑档案 {partner.partner_code}"}
    )


@perm_required("samples", "delete")
def partner_delete(request, pk):
    partner = get_object_or_404(Partner, pk=pk)
    if request.method == "POST":
        try:
            services.delete_partner(partner, _current_name(request), services.client_ip(request))
            messages.success(request, "档案已删除。")
        except services.BusinessError as exc:
            messages.error(request, str(exc))
        return redirect("samples:partner_list")
    return render(request, "samples/partner_confirm_delete.html", {"partner": partner})


# ---------------------------------------------------------------------------
# Excel 导出（沿用列表筛选条件）
# ---------------------------------------------------------------------------

@perm_required("samples", "read")
@guarded_export
def export_samples(request):
    from work_record.export_guard import export_too_large_response, max_export_rows

    flt = SampleFilter(request.GET)
    qs = flt.apply()
    # 并发闸门由 @guarded_export 提供；这里只处理行数上限
    # （openpyxl 全量在内存构建，不能一次导出几十万行）
    total_rows = qs.count()
    row_limit = max_export_rows()
    if total_rows > row_limit:
        return export_too_large_response(total_rows, row_limit)
    response = export_samples_xlsx(qs)
    # 导出行为留痕（写入 records.OperationLog，供审计异常检测统一读取）
    from records.audit import log_operation
    log_operation(request, "试样管理", "导出", "[试样管理]下的导出",
                  f"导出试样清单: {qs.count()} 条")
    return response
