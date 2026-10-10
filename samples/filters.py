"""样品台账多条件组合筛选。

台账列表与 Excel 导出共用本筛选器，确保导出数据与页面明细完全一致。
登记人为开放填写文本，按名称精确筛选。
"""

from datetime import datetime

from django.db.models import Q

from .models import Sample


def _parse_date(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


class SampleFilter:
    def __init__(self, data=None):
        data = data or {}
        self.keyword = data.get("keyword", "").strip()
        self.supplier_id = data.get("supplier", "").strip()
        self.status = data.get("status", "").strip()
        self.registrar = data.get("registrar", "").strip()
        self.date_from = _parse_date(data.get("date_from"))
        self.date_to = _parse_date(data.get("date_to"))
        self.show_deleted = data.get("show_deleted") == "1"

    def apply(self, queryset=None):
        qs = queryset if queryset is not None else Sample.objects.all()
        if not self.show_deleted:
            qs = qs.filter(is_deleted=False)

        if self.keyword:
            qs = qs.filter(
                Q(sample_no__icontains=self.keyword)
                | Q(material_name__icontains=self.keyword)
                | Q(model__icontains=self.keyword)
            )
        if self.supplier_id.isdigit():
            qs = qs.filter(supplier_id=int(self.supplier_id))
        if self.status in dict(Sample.Status.choices):
            qs = qs.filter(status=self.status)
        if self.registrar:
            qs = qs.filter(registrar=self.registrar)
        if self.date_from:
            qs = qs.filter(sample_date__gte=self.date_from)
        if self.date_to:
            qs = qs.filter(sample_date__lte=self.date_to)
        return qs

    @property
    def has_conditions(self):
        return any(
            [
                self.keyword,
                self.supplier_id,
                self.status,
                self.registrar,
                self.date_from,
                self.date_to,
            ]
        )

    def query_string(self):
        """保留筛选条件的查询串（用于导出、分页链接）。"""
        from urllib.parse import urlencode

        params = {}
        if self.keyword:
            params["keyword"] = self.keyword
        if self.supplier_id:
            params["supplier"] = self.supplier_id
        if self.status:
            params["status"] = self.status
        if self.registrar:
            params["registrar"] = self.registrar
        if self.date_from:
            params["date_from"] = self.date_from.isoformat()
        if self.date_to:
            params["date_to"] = self.date_to.isoformat()
        if self.show_deleted:
            params["show_deleted"] = "1"
        return urlencode(params)
