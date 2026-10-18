"""业务事务服务层。

所有写操作统一经此入口，确保：
1. 编号、轮次自动生成；
2. 状态机联动规则 100% 集中在本模块实现；
3. 每次新增/编辑/删除/状态变更同步写入 OperationLog 留痕。

视图层只负责表单收集与异常展示，不直接做状态变更。
operator 参数为开放填写的操作人姓名文本（本站无登录体系），可为 None。
"""

from django.db import transaction
from django.utils import timezone

from . import serialno
from .models import BatchRecord, OperationLog, Partner, ProofAttachment, Sample, TestRecord

import logging

logger = logging.getLogger(__name__)


class BusinessError(Exception):
    """业务规则校验错误，视图层捕获后以页面错误提示展示。"""


def client_ip(request):
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _log(operator, action, target_type, target_repr, summary, ip=None):
    OperationLog.objects.create(
        action=action,
        target_type=target_type,
        target_repr=target_repr[:200],
        summary=summary,
        operator=(operator or "").strip(),
        ip=ip,
    )


def _diff(old_obj, new_obj, fields, labels):
    """对比两个实例字段差异，生成中文变更摘要。"""
    parts = []
    for field in fields:
        old_val = getattr(old_obj, field)
        new_val = getattr(new_obj, field)
        if old_val != new_val:
            old_text = old_val if old_val not in (None, "") else "空"
            new_text = new_val if new_val not in (None, "") else "空"
            parts.append(f"{labels[field]}：「{old_text}」改为「{new_text}」")
    return "；".join(parts)


# ---------------------------------------------------------------------------
# 状态机：依据子记录全集推导主档状态
# ---------------------------------------------------------------------------

def _derive_status_from_records(sample):
    """依据试样/小批量记录全集推导样品应有状态（状态机纯函数化）。

    - 任一试样「不通过」或任一批次「不通过」 → 已终止
    - 存在「通过」批次 → 已正式导入
    - 存在小批量记录 → 小批量中
    - 最新一轮试样「通过」 → 小批量中
    - 存在试样记录 → 试样中
    - 无任何记录 → 待送样
    """
    tests = list(sample.test_records.order_by("round_no"))
    batches = list(sample.batch_records.order_by("round_no"))
    if any(t.review_conclusion == TestRecord.Conclusion.FAIL for t in tests):
        return Sample.Status.TERMINATED
    if any(b.batch_result == BatchRecord.Result.FAIL for b in batches):
        return Sample.Status.TERMINATED
    if any(b.batch_result == BatchRecord.Result.PASS for b in batches):
        return Sample.Status.IMPORTED
    if batches:
        return Sample.Status.SMALL_BATCH
    if tests:
        return (
            Sample.Status.SMALL_BATCH
            if tests[-1].review_conclusion == TestRecord.Conclusion.PASS
            else Sample.Status.TESTING
        )
    return Sample.Status.PENDING


def _resync_sample_status(sample, operator, ip=None, context=""):
    """子记录编辑/删除后，按记录全集重算并同步主档状态。

    - 「已终止」为终态：已终止样品不参与重算（手动终止不受记录编辑影响）；
    - 重算进入「已正式导入」时记录 imported_at / imported_by，退出时清空；
    - 重算进入「已终止」时，终止原因按系统判定格式生成。
    返回 True 表示发生了状态变更。
    """
    if sample.is_deleted or sample.status == Sample.Status.TERMINATED:
        return False

    sample = Sample.objects.select_for_update().get(pk=sample.pk)
    if sample.is_deleted or sample.status == Sample.Status.TERMINATED:
        return False

    old_status_text = sample.get_status_display()
    new_status = _derive_status_from_records(sample)
    if new_status == sample.status:
        return False

    if new_status == Sample.Status.TERMINATED:
        fail_test = next(
            (
                t
                for t in sample.test_records.order_by("-round_no")
                if t.review_conclusion == TestRecord.Conclusion.FAIL
            ),
            None,
        )
        fail_batch = next(
            (
                b
                for b in sample.batch_records.order_by("-round_no")
                if b.batch_result == BatchRecord.Result.FAIL
            ),
            None,
        )
        if fail_test:
            sample.terminate_reason = f"系统判定：第{fail_test.round_no}轮试样评审不通过"
        else:
            sample.terminate_reason = f"系统判定：第{fail_batch.round_no}批小批量验证不通过"
        sample.terminated_by = (operator or "").strip()
        sample.terminated_at = timezone.now()
    elif new_status == Sample.Status.IMPORTED:
        sample.imported_by = (operator or "").strip()
        sample.imported_at = timezone.now()
    elif sample.status == Sample.Status.IMPORTED:
        sample.imported_by = ""
        sample.imported_at = None

    sample.status = new_status
    sample.save()
    _log(
        operator,
        OperationLog.Action.STATUS,
        "样品主档",
        sample.sample_no,
        f"{context}，状态「{old_status_text}」→「{sample.get_status_display()}」",
        ip,
    )
    return True


# ---------------------------------------------------------------------------
# 供应商档案
# ---------------------------------------------------------------------------

@transaction.atomic
def create_partner(partner, operator=None, ip=None):
    partner.partner_code = serialno.next_yearly_serial(Partner, "partner_code")
    partner.full_clean()
    partner.save()
    _log(
        operator,
        OperationLog.Action.CREATE,
        "供应商档案",
        str(partner),
        f"新增供应商档案：{partner.name}",
        ip,
    )
    return partner


@transaction.atomic
def update_partner(partner, operator=None, ip=None):
    old = Partner.objects.get(pk=partner.pk)
    tracked = ["name", "contact_person", "contact_info", "status"]
    labels = {
        "name": "名称",
        "contact_person": "联系人",
        "contact_info": "联系方式",
        "status": "合作状态",
    }
    # 中文展示值对比
    old_display = {
        "status": old.get_status_display(),
    }
    new_display = {
        "status": partner.get_status_display(),
    }
    changed = []
    for field in tracked:
        ov = old_display.get(field, getattr(old, field))
        nv = new_display.get(field, getattr(partner, field))
        if ov != nv:
            changed.append(
                f"{labels[field]}：「{ov or '空'}」改为「{nv or '空'}」"
            )
    partner.full_clean()
    partner.save()
    if changed:
        _log(
            operator,
            OperationLog.Action.UPDATE,
            "供应商档案",
            str(partner),
            "；".join(changed),
            ip,
        )
    return partner


@transaction.atomic
def delete_partner(partner, operator=None, ip=None):
    """已被样品引用的档案禁止删除（保障数据稳定）。"""
    if partner.is_referenced:
        raise BusinessError(
            f"档案「{partner.name}」已被样品台账引用，禁止删除；如不再合作请将状态改为「已停用」。"
        )
    repr_text = str(partner)
    name = partner.name
    partner.delete()
    _log(
        operator,
        OperationLog.Action.DELETE,
        "供应商档案",
        repr_text,
        f"删除档案：{name}",
        ip,
    )


# ---------------------------------------------------------------------------
# 样品主档
# ---------------------------------------------------------------------------

@transaction.atomic
def create_sample(sample, operator=None, ip=None):
    sample.sample_no = serialno.next_daily_serial(Sample, "sample_no", "S")
    sample.status = Sample.Status.PENDING
    sample.registered_at = timezone.now()
    sample.full_clean()
    sample.save()
    _log(
        operator,
        OperationLog.Action.CREATE,
        "样品主档",
        sample.sample_no,
        (
            f"送样登记：物料「{sample.material_name}」型号「{sample.model}」，"
            f"供应商「{sample.supplier.name if sample.supplier else '未指定'}」，"
            f"送样时间 {sample.sample_date:%Y-%m-%d}"
        ),
        ip,
    )
    return sample


@transaction.atomic
def update_sample(sample, operator=None, ip=None):
    if sample.is_deleted:
        raise BusinessError("样品已删除，请先恢复后再编辑。")
    old = Sample.objects.get(pk=sample.pk)
    tracked = ["material_name", "model", "sample_date", "spec", "incoming_qty", "supplier", "supplier_type"]
    labels = {
        "material_name": "物料名称",
        "model": "型号",
        "sample_date": "送样时间",
        "spec": "物料规格",
        "incoming_qty": "来料数量",
        "supplier": "供应商",
        "supplier_type": "供应商类型",
    }
    sample.full_clean()
    sample.save()
    # supplier_type 对比用显示值而非 raw value
    old_display = {"supplier_type": old.get_supplier_type_display()}
    new_display = {"supplier_type": sample.get_supplier_type_display()}
    changed = []
    for field in tracked:
        ov = old_display.get(field, getattr(old, field))
        nv = new_display.get(field, getattr(sample, field))
        if ov != nv:
            changed.append(f"{labels[field]}：「{ov or '空'}」改为「{nv or '空'}」")
    summary = "；".join(changed)
    if summary:
        _log(operator, OperationLog.Action.UPDATE, "样品主档", sample.sample_no, summary, ip)
    return sample


@transaction.atomic
def soft_delete_sample(sample, operator=None, ip=None):
    if sample.is_deleted:
        raise BusinessError("样品已处于删除状态。")
    sample.is_deleted = True
    sample.deleted_by = (operator or "").strip()
    sample.deleted_at = timezone.now()
    sample.save(update_fields=["is_deleted", "deleted_by", "deleted_at", "updated_at"])
    _log(
        operator,
        OperationLog.Action.DELETE,
        "样品主档",
        sample.sample_no,
        f"删除样品：{sample.material_name}/{sample.model}（软删除，可恢复）",
        ip,
    )


@transaction.atomic
def restore_sample(sample, operator=None, ip=None):
    if not sample.is_deleted:
        raise BusinessError("样品未处于删除状态。")
    sample.is_deleted = False
    sample.deleted_by = ""
    sample.deleted_at = None
    sample.save(update_fields=["is_deleted", "deleted_by", "deleted_at", "updated_at"])
    _log(
        operator,
        OperationLog.Action.UPDATE,
        "样品主档",
        sample.sample_no,
        "恢复已删除样品",
        ip,
    )


@transaction.atomic
def terminate_sample(sample, operator, reason, ip=None):
    """任意非终态手动终止。"""
    if sample.is_deleted:
        raise BusinessError("样品已删除，不能终止。")
    if sample.is_terminal:
        raise BusinessError(f"样品已处于「{sample.get_status_display()}」终态，不能终止。")
    reason = (reason or "").strip()
    if not reason:
        raise BusinessError("终止原因为必填项。")
    old_status = sample.get_status_display()
    sample.status = Sample.Status.TERMINATED
    sample.terminate_reason = reason
    sample.terminated_by = (operator or "").strip()
    sample.terminated_at = timezone.now()
    sample.save(
        update_fields=[
            "status",
            "terminate_reason",
            "terminated_by",
            "terminated_at",
            "updated_at",
        ]
    )
    _log(
        operator,
        OperationLog.Action.STATUS,
        "样品主档",
        sample.sample_no,
        f"手动终止：状态「{old_status}」→「已终止」，终止原因：{reason}",
        ip,
    )


# ---------------------------------------------------------------------------
# 试样记录（多轮试产）
# ---------------------------------------------------------------------------

@transaction.atomic
def create_test_record(record, operator=None, ip=None, terminate_reason=""):
    sample = Sample.objects.select_for_update().get(pk=record.sample_id)
    if sample.is_deleted:
        raise BusinessError("样品已删除，不能新增试样记录。")
    if sample.status not in (Sample.Status.PENDING, Sample.Status.TESTING):
        raise BusinessError(
            f"样品当前状态为「{sample.get_status_display()}」，不能新增试样记录。"
        )

    record.sample = sample
    record.round_no = serialno.next_round(TestRecord, sample)
    record.test_no = serialno.next_daily_serial(TestRecord, "test_no", "T")
    record.operated_at = timezone.now()

    old_status_text = sample.get_status_display()
    conclusion = record.review_conclusion
    if conclusion == TestRecord.Conclusion.PASS:
        sample.status = Sample.Status.SMALL_BATCH
    elif conclusion == TestRecord.Conclusion.FAIL:
        reason = (terminate_reason or "").strip()
        if not reason:
            raise BusinessError("评审结论为「不通过」时，必须填写终止原因。")
        sample.status = Sample.Status.TERMINATED
        sample.terminate_reason = reason
        sample.terminated_by = (operator or "").strip()
        sample.terminated_at = timezone.now()
    else:  # 待改进：保持/进入试样中
        sample.status = Sample.Status.TESTING

    record.full_clean()
    sample.save()
    record.save()

    _log(
        operator,
        OperationLog.Action.CREATE,
        "试样记录",
        record.test_no,
        (
            f"样品 {sample.sample_no} 新增第{record.round_no}轮试样，"
            f"评审结论「{record.get_review_conclusion_display()}」，"
            f"状态「{old_status_text}」→「{sample.get_status_display()}」"
        ),
        ip,
    )
    return record


@transaction.atomic
def update_test_record(record, operator=None, ip=None):
    """编辑试样记录：更新数据，并按记录全集重算主档状态（编辑同样驱动状态机）。"""
    old = TestRecord.objects.get(pk=record.pk)
    tracked = [
        "test_date",
        "test_result",
        "review_conclusion",
        "report_code",
        "notified",
        "package_type",
        "incoming_date",
        "remark",
    ]
    labels = {
        "test_date": "试产日期",
        "test_result": "测试结果",
        "review_conclusion": "评审结论",
        "report_code": "验证报告编码",
        "notified": "是否通知责任人",
        "package_type": "封装形式",
        "incoming_date": "来料日期",
        "remark": "备注",
    }
    record.full_clean()
    record.save()
    summary = _diff(old, record, tracked, labels)
    if summary:
        _log(
            operator,
            OperationLog.Action.UPDATE,
            "试样记录",
            record.test_no,
            f"样品 {record.sample.sample_no} 第{record.round_no}轮试样编辑：{summary}",
            ip,
        )
    _resync_sample_status(
        record.sample,
        operator or record.operator,
        ip,
        context=f"编辑第{record.round_no}轮试样记录（评审结论改为「{record.get_review_conclusion_display()}」）",
    )
    return record


@transaction.atomic
def delete_test_record(record, operator=None, ip=None):
    text = f"{record.test_no}（样品 {record.sample.sample_no} 第{record.round_no}轮）"
    sample = record.sample
    round_no = record.round_no
    record.delete()
    _log(operator, OperationLog.Action.DELETE, "试样记录", text, "删除试样记录", ip)
    _resync_sample_status(
        sample,
        operator,
        ip,
        context=f"删除第{round_no}轮试样记录",
    )


# ---------------------------------------------------------------------------
# 小批量记录（多批验证）
# ---------------------------------------------------------------------------

@transaction.atomic
def create_batch_record(record, operator=None, ip=None, terminate_reason=""):
    sample = Sample.objects.select_for_update().get(pk=record.sample_id)
    if sample.is_deleted:
        raise BusinessError("样品已删除，不能新增小批量记录。")
    if sample.status != Sample.Status.SMALL_BATCH:
        raise BusinessError(
            f"样品当前状态为「{sample.get_status_display()}」，仅「小批量中」状态可新增小批量记录。"
        )

    record.sample = sample
    record.round_no = serialno.next_round(BatchRecord, sample)
    record.batch_no = serialno.next_daily_serial(BatchRecord, "batch_no", "B")
    record.operated_at = timezone.now()

    old_status_text = sample.get_status_display()
    result = record.batch_result
    if result == BatchRecord.Result.PASS:
        sample.status = Sample.Status.IMPORTED
        sample.imported_by = (operator or "").strip()
        sample.imported_at = timezone.now()
    elif result == BatchRecord.Result.FAIL:
        reason = (terminate_reason or "").strip()
        if not reason:
            raise BusinessError("小批量结果为「不通过」时，必须填写终止原因。")
        sample.status = Sample.Status.TERMINATED
        sample.terminate_reason = reason
        sample.terminated_by = (operator or "").strip()
        sample.terminated_at = timezone.now()
    # 进行中：保持小批量中

    record.full_clean()
    sample.save()
    record.save()

    _log(
        operator,
        OperationLog.Action.CREATE,
        "小批量记录",
        record.batch_no,
        (
            f"样品 {sample.sample_no} 新增第{record.round_no}批小批量验证，"
            f"结果「{record.get_batch_result_display()}」，"
            f"状态「{old_status_text}」→「{sample.get_status_display()}」"
        ),
        ip,
    )
    return record


@transaction.atomic
def update_batch_record(record, operator=None, ip=None):
    """编辑小批量记录：更新数据，并按记录全集重算主档状态（编辑同样驱动状态机）。"""
    old = BatchRecord.objects.get(pk=record.pk)
    tracked = [
        "ordered",
        "batch_result",
        "kingdee_entered",
        "unit_price",
        "alternative_model",
        "remark",
    ]
    labels = {
        "ordered": "是否下单",
        "batch_result": "小批量结果",
        "kingdee_entered": "是否录入金蝶",
        "unit_price": "试样单价",
        "alternative_model": "可替代型号",
        "remark": "批次备注",
    }
    record.full_clean()
    record.save()
    summary = _diff(old, record, tracked, labels)
    if summary:
        _log(
            operator,
            OperationLog.Action.UPDATE,
            "小批量记录",
            record.batch_no,
            f"样品 {record.sample.sample_no} 第{record.round_no}批小批量编辑：{summary}",
            ip,
        )
    _resync_sample_status(
        record.sample,
        operator or record.operator,
        ip,
        context=f"编辑第{record.round_no}批小批量记录（结果改为「{record.get_batch_result_display()}」）",
    )
    return record


@transaction.atomic
def delete_batch_record(record, operator=None, ip=None):
    text = f"{record.batch_no}（样品 {record.sample.sample_no} 第{record.round_no}批）"
    sample = record.sample
    round_no = record.round_no
    record.delete()
    _log(operator, OperationLog.Action.DELETE, "小批量记录", text, "删除小批量记录", ip)
    _resync_sample_status(
        sample,
        operator,
        ip,
        context=f"删除第{round_no}批小批量记录",
    )


# ---------------------------------------------------------------------------
# 证明材料（业务流程关键节点凭证上传/删除）
# ---------------------------------------------------------------------------

@transaction.atomic
def upload_proof(sample, stage, file_obj, operator=None, ip=None, remark=""):
    """上传业务流程证明材料。

    sample   : 样品主档
    stage    : ProofAttachment.Stage 取值
    file_obj : 已通过表单校验的 UploadedFile
    remark   : 可选备注
    """
    if sample.is_deleted:
        raise BusinessError("样品已删除，不能上传证明材料。")
    if stage not in ProofAttachment.Stage.values:
        raise BusinessError(f"无效的证明材料阶段：{stage}")

    attachment = ProofAttachment.objects.create(
        sample=sample,
        stage=stage,
        file=file_obj,
        original_name=file_obj.name,
        uploaded_by=(operator or "").strip(),
        remark=(remark or "").strip(),
    )
    stage_label = attachment.get_stage_display()
    _log(
        operator,
        OperationLog.Action.CREATE,
        "证明材料",
        f"{sample.sample_no} · {stage_label}",
        (
            f"样品 {sample.sample_no} 上传证明材料：{stage_label}，"
            f"文件「{attachment.original_name}」"
        ),
        ip,
    )
    return attachment


@transaction.atomic
def delete_proof(attachment, operator=None, ip=None):
    """删除证明材料并清理存储文件。"""
    sample_no = attachment.sample.sample_no if attachment.sample_id else "—"
    stage_label = attachment.get_stage_display()
    file_name = attachment.original_name
    # 先取文件引用再删除记录，避免 db 删除后取不到 storage
    storage = attachment.file.storage
    file_path = attachment.file.name
    attachment.delete()
    try:
        if file_path and storage.exists(file_path):
            storage.delete(file_path)
    except Exception:  # 文件系统异常不应阻断删除流程
        pass
    _log(
        operator,
        OperationLog.Action.DELETE,
        "证明材料",
        f"{sample_no} · {stage_label}",
        f"删除证明材料：{stage_label}，文件「{file_name}」",
        ip,
    )
