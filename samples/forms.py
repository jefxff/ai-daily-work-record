"""业务表单：服务端必填校验 + Bootstrap 样式（与全站 form-control 风格一致）。

编号、轮次、操作时间、状态等字段均不出现在表单中，
由 services 层按规则自动生成与联动。
登记人/操作人/上传人为开放填写文本（本站无登录体系）。
"""

import os

from django import forms
from django.conf import settings

from .models import AuditRecord, BatchRecord, Partner, ProofAttachment, Sample, TestRecord


class BootstrapModelForm(forms.ModelForm):
    """统一为控件添加 Bootstrap 样式。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs["class"] = "form-check-input"
            elif isinstance(widget, forms.Select):
                widget.attrs["class"] = "form-select"
            else:
                widget.attrs["class"] = "form-control"


class PartnerForm(BootstrapModelForm):
    class Meta:
        model = Partner
        fields = ["name", "contact_person", "contact_info", "status"]


class SampleForm(BootstrapModelForm):
    registrar = forms.CharField(
        label="登记人",
        max_length=50,
        required=True,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "请填写登记人姓名"}),
    )

    class Meta:
        model = Sample
        fields = [
            "material_name",
            "model",
            "sample_date",
            "spec",
            "incoming_qty",
            "supplier",
            "supplier_type",
        ]
        widgets = {
            "sample_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        suppliers = Partner.objects.filter(status=Partner.CooperationStatus.ACTIVE)
        # 编辑时保留当前已停用但已关联的档案
        if self.instance.pk and self.instance.supplier_id:
            suppliers = suppliers | Partner.objects.filter(pk=self.instance.supplier_id)
        self.fields["supplier"].queryset = suppliers.distinct()
        self.fields["supplier"].empty_label = "— 无 —"


class TestRecordForm(BootstrapModelForm):
    """试样记录表单；结论为「不通过」时终止原因必填（视图+服务层双重校验）。"""

    terminate_reason = forms.CharField(
        label="终止原因",
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        help_text="评审结论选择「不通过」时必填，样品将直接转为「已终止」。",
    )

    class Meta:
        model = TestRecord
        fields = [
            "test_date",
            "package_type",
            "incoming_date",
            "test_result",
            "review_conclusion",
            "report_code",
            "notified",
            "operator",
            "remark",
        ]
        widgets = {
            "test_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "incoming_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "test_result": forms.Textarea(attrs={"rows": 4}),
            "remark": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["operator"].required = True
        self.fields["operator"].widget.attrs["placeholder"] = "请填写操作人姓名"

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("review_conclusion") == TestRecord.Conclusion.FAIL:
            if not (cleaned.get("terminate_reason") or "").strip():
                self.add_error("terminate_reason", "评审结论为「不通过」时，必须填写终止原因。")
        return cleaned


class BatchRecordForm(BootstrapModelForm):
    """小批量记录表单；结果为「不通过」时终止原因必填。"""

    terminate_reason = forms.CharField(
        label="终止原因",
        required=False,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        help_text="小批量结果选择「不通过」时必填，样品将直接转为「已终止」。",
    )

    class Meta:
        model = BatchRecord
        fields = [
            "ordered",
            "unit_price",
            "batch_result",
            "kingdee_entered",
            "alternative_model",
            "operator",
            "remark",
        ]
        widgets = {
            "remark": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["operator"].required = True
        self.fields["operator"].widget.attrs["placeholder"] = "请填写操作人姓名"

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("batch_result") == BatchRecord.Result.FAIL:
            if not (cleaned.get("terminate_reason") or "").strip():
                self.add_error("terminate_reason", "小批量结果为「不通过」时，必须填写终止原因。")
        return cleaned


class AuditRecordForm(BootstrapModelForm):
    """审厂记录表单（多轮审厂，支持文件上传）。"""

    auditor = forms.CharField(
        label="操作人",
        max_length=50,
        required=True,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "请填写操作人姓名"}),
    )

    class Meta:
        model = AuditRecord
        fields = [
            "audit_date",
            "audit_result",
            "audit_remark",
            "file",
        ]
        widgets = {
            "audit_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "audit_remark": forms.Textarea(attrs={"rows": 2, "placeholder": "可选：审厂发现问题、整改要求等"}),
            "file": forms.ClearableFileInput(
                attrs={
                    "class": "form-control",
                    "accept": "image/*,.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.csv,.zip,.rar,.7z",
                    "placeholder": "上传审核记录文件",
                }
            ),
        }

    def clean_file(self):
        from .validators import validate_upload
        f = self.cleaned_data.get("file")
        return validate_upload(f, field_label="审核记录文件")


class TerminateForm(forms.Form):
    reason = forms.CharField(
        label="终止原因",
        required=True,
        widget=forms.Textarea(
            attrs={"class": "form-control", "rows": 4, "placeholder": "请填写终止原因（必填）"}
        ),
    )
    operator = forms.CharField(
        label="操作人",
        max_length=50,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "可选：填写操作人姓名"}),
    )


class ProofAttachmentForm(forms.ModelForm):
    """证明材料上传表单。

    仅暴露 file / remark / uploaded_by 三个字段；sample/stage 由视图+服务层注入。
    严格校验文件类型与大小，确保上传的关键凭证清晰可辨识。
    """

    uploaded_by = forms.CharField(
        label="上传人",
        max_length=50,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "可选：填写上传人姓名"}),
    )

    class Meta:
        model = ProofAttachment
        fields = ["file", "remark"]
        widgets = {
            "file": forms.ClearableFileInput(
                attrs={
                    "class": "form-control",
                    "accept": "image/*,.pdf,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.txt,.csv,.zip,.rar,.7z",
                    "placeholder": "选择文件（≤20MB）",
                }
            ),
            "remark": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 2,
                    "placeholder": "可选：注明订单号/日期/人员等关键信息",
                }
            ),
        }
        labels = {
            "file": "证明文件",
            "remark": "备注",
        }

    def clean_file(self):
        from .validators import validate_upload
        f = self.cleaned_data.get("file")
        return validate_upload(f, field_label="证明文件")
