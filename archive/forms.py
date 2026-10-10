"""档案管理模块表单。"""

from django import forms
from django.utils import timezone

from .models import Archive, BorrowRecord, Cabinet, Category


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
        fields = ["code", "name", "cabinet", "layer", "cell", "file", "category", "tags", "description"]
        widgets = {
            "code": forms.TextInput(attrs={"class": "form-control"}),
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "cabinet": forms.Select(attrs={"class": "form-select"}),
            "layer": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "cell": forms.NumberInput(attrs={"class": "form-control", "min": 1}),
            "file": forms.ClearableFileInput(attrs={"class": "form-control"}),
            "category": forms.Select(attrs={"class": "form-select"}),
            "tags": forms.TextInput(attrs={"class": "form-control", "placeholder": "多个标签用逗号分隔"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }
        labels = {
            "file": "电子档案（PDF/图片/Word/Excel 等）",
        }

    def __init__(self, *args, exclude_cell: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].required = False
        self.fields["file"].required = False
        if exclude_cell:
            # 跨空位移动时只允许改柜/层/格
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
            # 检查目标格是否已被其他档案占用
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
            "borrower": forms.Select(attrs={"class": "form-select"}),
            "due_time": forms.DateInput(
                attrs={"class": "form-control", "type": "date"}, format="%Y-%m-%d"
            ),
            "remark": forms.TextInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["borrower"].required = True
        if not self.initial.get("due_time"):
            # 默认 7 天后归还
            self.fields["due_time"].initial = (timezone.now() + timezone.timedelta(days=7)).date()
