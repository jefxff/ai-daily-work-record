"""档案管理模块表单。"""

import os

from django import forms
from django.core.exceptions import ValidationError

from work_record.file_validation import magic_ok, read_head
from django.utils import timezone

# 附件校验（扩展名 + 大小 + 文件头）收敛到 validators.py，这里保留原有导出名
from .validators import validate_archive_file  # noqa: F401

from .models import (
    AnnualReview,
    Archive,
    BorrowRecord,
    Cabinet,
    Category,
    Ledger,
    License,
    LicenseBorrowRecord,
)


# 证照扫描件：仅允许图片/PDF，禁止 HTML/SVG 等可执行内容（审计 P1-2）
LICENSE_SCAN_ALLOWED_EXTS = {"PNG", "JPG", "JPEG", "GIF", "BMP", "WEBP", "PDF"}
LICENSE_SCAN_MAX_SIZE = 20 * 1024 * 1024

# 文件头判定统一走 work_record.file_validation（此前这里是第三份 magic 表，
# 且比同目录 validators.py 更严：PNG 查 8 字节 vs 4 字节 —— 同一类文件两个口径）


def validate_license_scan(f) -> None:
    """校验证照扫描件：扩展名白名单 + 文件头 + 大小。"""
    ext = os.path.splitext(getattr(f, "name", "") or "")[1].lstrip(".").upper()
    if ext not in LICENSE_SCAN_ALLOWED_EXTS:
        raise ValidationError(
            f"不支持的文件类型「{ext or '无扩展名'}」（仅允许图片/PDF）"
        )
    if f.size > LICENSE_SCAN_MAX_SIZE:
        raise ValidationError(
            f"文件大小 {f.size / 1024 / 1024:.1f}MB 超过上限 "
            f"{LICENSE_SCAN_MAX_SIZE // 1024 // 1024}MB"
        )
    if not magic_ok(ext, read_head(f, 16)):
        raise ValidationError("文件内容与扩展名不符，已拒绝上传")


class LedgerForm(forms.ModelForm):
    """台账表单（新建/编辑共用）。"""

    class Meta:
        model = Ledger
        fields = ["name", "description", "sort_order"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "placeholder": "如：专利、商标相关合同"}),
            "description": forms.TextInput(attrs={"class": "form-control", "placeholder": "台账说明（可选）"}),
            "sort_order": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
        }


class CabinetForm(forms.ModelForm):
    """档案柜表单（各层格数由视图解析 layer_cell_N 并同步层记录）。"""

    class Meta:
        model = Cabinet
        fields = ["code", "location", "layers"]
        widgets = {
            "code": forms.TextInput(attrs={"class": "form-control", "placeholder": "如 A01"}),
            "location": forms.TextInput(attrs={"class": "form-control", "placeholder": "如 3楼档案室"}),
            "layers": forms.NumberInput(attrs={"class": "form-control", "min": 1, "max": 12, "value": 4}),
        }
        labels = {
            "code": "档案柜编号",
            "location": "所在位置",
            "layers": "层数",
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
        # 父类别选项排除自身与全部后代：仅排除自身挡不住 A→B→A 这类两次编辑成环
        qs = Category.objects.all()
        if self.instance and self.instance.pk:
            blocked = self._descendant_ids(self.instance.pk) | {self.instance.pk}
            qs = qs.exclude(pk__in=blocked)
        self.fields["parent"].queryset = qs
        self.fields["parent"].required = False
        self.fields["parent"].empty_label = "— 顶级类别 —"

    @staticmethod
    def _descendant_ids(root_pk) -> set:
        """广度优先取后代 id，不递归 children.all()，避免把整棵树读进内存。"""
        ids, frontier = set(), [root_pk]
        while frontier:
            children = list(
                Category.objects.filter(parent_id__in=frontier).values_list("pk", flat=True)
            )
            frontier = [i for i in children if i not in ids]
            ids.update(frontier)
        return ids



class ArchiveForm(forms.ModelForm):
    """档案表单（录入/编辑共用）。"""

    class Meta:
        model = Archive
        fields = [
            "code", "name", "ledger", "cabinet", "layer", "cell", "location_code",
            "counterpart", "contract_no", "amount", "sign_date", "expire_date",
            "department", "security_level", "archive_label", "handler", "copies",
            "attachment_desc",
            "category", "tags", "description",
        ]
        widgets = {
            "code": forms.TextInput(attrs={"class": "form-control"}),
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "ledger": forms.Select(attrs={"class": "form-select"}),
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
            "category": forms.Select(attrs={"class": "form-select"}),
            "tags": forms.TextInput(attrs={"class": "form-control", "placeholder": "多个标签用逗号分隔"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, exclude_cell: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        for f in ("category", "ledger", "cabinet", "layer", "cell", "location_code",
                  "counterpart", "contract_no", "amount", "sign_date", "expire_date",
                  "department", "security_level", "archive_label", "handler", "copies",
                  "attachment_desc"):
            self.fields[f].required = False
        if exclude_cell:
            for f in ("code", "name", "category", "tags", "description"):
                self.fields[f].widget = forms.HiddenInput()

    def clean(self):
        cleaned = super().clean()
        cabinet = cleaned.get("cabinet")
        layer = cleaned.get("layer")
        cell = cleaned.get("cell")
        if cabinet and layer and cell:
            if layer > cabinet.layers:
                self.add_error("layer", f"该档案柜只有 {cabinet.layers} 层")
            else:
                cells = cabinet.cells_of_layer(layer)
                if cells is not None and cell > cells:
                    self.add_error("cell", f"该柜第 {layer} 层只有 {cells} 格")
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
        self.fields["scan_file"].validators.append(validate_license_scan)


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
