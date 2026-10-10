"""档案管理模块表单。"""

from django import forms
from django.utils import timezone

from .models import Archive, BorrowRecord, Cabinet, Category, License, LicenseBorrowRecord, AnnualReview


class CabinetForm(forms.ModelForm):
    """档案柜表单。"""

    class Meta:
        model = Cabinet
        fields = ["code", "location", "layers", "cells_per_layer"]
        widgets = {
            "code": forms.TextInput(attrs={"class": "form-control", "placeholder": "如 A01"}),
            "location": forms.TextInput(attrs={"class": "form-control", "placeholder": "如 3楼档案室"}),
            "layers": forms.Select(attrs={"class": "form-select"}),
            "cells_per_layer": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
        }
        labels = {
            "code": "档案柜编号",
            "location": "所在位置",
            "layers": "层数",
            "cells_per_layer": "每层格数",
        }


class CategoryForm(forms.ModelForm):
    """档案类别表单。"""

    class Meta:
        model = Category
        fields = ["name", "parent", "sort_order"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "parent": forms.Select(attrs={"class": "form-select"}),
            "sort_order": forms.NumberInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 父类别选项排除自身，避免循环引用
        qs = Category.objects.all()
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        self.fields["parent"].queryset = qs
        self.fields["parent"].required = False
        self.fields["parent"].empty_label = "— 顶级类别 —"


class ArchiveForm(forms.ModelForm):
    """档案表单（录入/编辑共用）。"""

    class Meta:
        model = Archive
        fields = [
            "code", "name", "cabinet", "layer", "cell", "location_code",
            "counterpart", "contract_no", "amount", "sign_date", "expire_date",
            "department", "security_level", "archive_label", "handler", "copies",
            "attachment_desc",
            "file", "category", "tags", "description",
        ]
        widgets = {
            "code": forms.TextInput(attrs={"class": "form-control"}),
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "cabinet": forms.Select(attrs={"class": "form-select"}),
            "layer": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "cell": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "location_code": forms.TextInput(attrs={"class": "form-control", "placeholder": "如 09-3-7-1"}),
            "counterpart": forms.TextInput(attrs={"class": "form-control", "placeholder": "合同相对方/供应商/发文单位"}),
            "contract_no": forms.TextInput(attrs={"class": "form-control"}),
            "amount": forms.TextInput(attrs={"class": "form-control", "placeholder": "如 135956.8 或 300万"}),
            "sign_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}, format="%Y-%m-%d"),
            "expire_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}, format="%Y-%m-%d"),
            "department": forms.TextInput(attrs={"class": "form-control", "placeholder": "如 行政部、采购部"}),
            "security_level": forms.TextInput(attrs={"class": "form-control", "placeholder": "机密/一般/..."}),
            "archive_label": forms.TextInput(attrs={"class": "form-control", "placeholder": "如 专利合同(一)"}),
            "handler": forms.TextInput(attrs={"class": "form-control"}),
            "copies": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "attachment_desc": forms.TextInput(attrs={"class": "form-control"}),
            "file": forms.ClearableFileInput(attrs={"class": "form-control"}),
            "category": forms.Select(attrs={"class": "form-select"}),
            "tags": forms.TextInput(attrs={"class": "form-control", "placeholder": "多个标签用逗号分隔"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }
        labels = {
            "file": "电子档案",
        }

    def __init__(self, *args, exclude_cell: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        for f in ("category", "file", "cabinet", "layer", "cell", "location_code",
                  "counterpart", "contract_no", "amount", "sign_date", "expire_date",
                  "department", "security_level", "archive_label", "handler", "copies",
                  "attachment_desc"):
            self.fields[f].required = False
        if exclude_cell:
            for f in ("code", "name", "file", "category", "tags", "description"):
                self.fields[f].widget = forms.HiddenInput()

    def clean(self):
        cleaned = super().clean()
        cabinet = cleaned.get("cabinet")
        layer = cleaned.get("layer")
        cell = cleaned.get("cell")
        if cabinet and layer and cell:
            if layer > cabinet.layers:
                self.add_error("layer", f"该档案柜只有 {cabinet.layers} 层")
            if cell > cabinet.cells_per_layer:
                self.add_error("cell", f"该层只有 {cabinet.cells_per_layer} 格")
            qs = Archive.objects.filter(cabinet=cabinet, layer=layer, cell=cell)
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                self.add_error(None, f"该位置（{cabinet.code}-{layer}层-{cell}格）已被占用")
        return cleaned


class BorrowForm(forms.ModelForm):
    """借阅登记表单。"""

    class Meta:
        model = BorrowRecord
        fields = ["borrower", "due_time", "remark"]
        widgets = {
            "borrower": forms.TextInput(attrs={"class": "form-control", "placeholder": "借阅人姓名（可为系统外人员）"}),
            "due_time": forms.DateInput(
                attrs={"class": "form-control", "type": "date"}, format="%Y-%m-%d"
            ),
            "remark": forms.TextInput(attrs={"class": "form-control", "placeholder": "备注，如借阅用途"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["borrower"].required = True
        if not self.initial.get("due_time"):
            # 默认 7 天后归还
            self.fields["due_time"].initial = (timezone.now() + timezone.timedelta(days=7)).date()


# ==================== 证照管理表单 ====================


class LicenseForm(forms.ModelForm):
    """证照表单（录入/编辑共用）。"""

    class Meta:
        model = License
        fields = [
            "code", "name", "holder", "issuer",
            "issue_date", "expire_date",
            "review_cycle", "next_review_date",
            "scan_file", "remark",
        ]
        widgets = {
            "code": forms.TextInput(attrs={"class": "form-control", "placeholder": "如 LIC2026-001"}),
            "name": forms.TextInput(attrs={"class": "form-control", "placeholder": "如 营业执照"}),
            "holder": forms.TextInput(attrs={"class": "form-control", "placeholder": "持证单位全称"}),
            "issuer": forms.TextInput(attrs={"class": "form-control", "placeholder": "发证机关"}),
            "issue_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}, format="%Y-%m-%d"),
            "expire_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}, format="%Y-%m-%d"),
            "review_cycle": forms.NumberInput(attrs={"class": "form-control", "min": 0, "placeholder": "0=不需年审，12=每年一次"}),
            "next_review_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}, format="%Y-%m-%d"),
            "scan_file": forms.ClearableFileInput(attrs={"class": "form-control"}),
            "remark": forms.Textarea(attrs={"class": "form-control", "rows": 3, "placeholder": "备注信息"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in ("holder", "issuer", "issue_date", "expire_date",
                  "next_review_date", "scan_file", "remark"):
            self.fields[f].required = False


class AnnualReviewForm(forms.ModelForm):
    """年审登记表单。"""

    class Meta:
        model = AnnualReview
        fields = ["review_date", "result", "reviewer", "new_expire_date", "remark"]
        widgets = {
            "review_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}, format="%Y-%m-%d"),
            "result": forms.Select(attrs={"class": "form-select"}),
            "reviewer": forms.TextInput(attrs={"class": "form-control", "placeholder": "经办人姓名"}),
            "new_expire_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}, format="%Y-%m-%d"),
            "remark": forms.TextInput(attrs={"class": "form-control", "placeholder": "年审备注"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reviewer"].required = True
        self.fields["new_expire_date"].required = False
        self.fields["remark"].required = False


class LicenseBorrowForm(forms.ModelForm):
    """证照借阅登记表单。"""

    class Meta:
        model = LicenseBorrowRecord
        fields = ["borrower", "due_time", "remark"]
        widgets = {
            "borrower": forms.TextInput(attrs={"class": "form-control", "placeholder": "借阅人姓名（可为系统外人员）"}),
            "due_time": forms.DateInput(attrs={"class": "form-control", "type": "date"}, format="%Y-%m-%d"),
            "remark": forms.TextInput(attrs={"class": "form-control", "placeholder": "备注，如借阅用途"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["borrower"].required = True
        if not self.initial.get("due_time"):
            self.fields["due_time"].initial = (timezone.now() + timezone.timedelta(days=7)).date()
